"""Intelligent in-context context compression manager for Agno agents.

Unlike standard Agno compression which runs an expensive, slow LLM call per tool
result (destroying KV prompt caches and causing 15s+ timeouts), ``SmartCompressionManager``:
1. Evaluates total context tokens accurately using ``tiktoken`` before the LLM call.
2. Leverages in-context cache prefix: sends the checkpoint generation prompt at the
   end of the existing conversation so the LLM hits 100% of the KV cache.
3. Reorganizes in-flight messages in-place: preserves instructions, adds a dense
   ``# CONTEXT CHECKPOINT``, and keeps the active turn's tool call & result intact.
4. Traces compression via OpenTelemetry / OTLP with a dedicated ``context_compression`` span.
5. Supports rich ``debug_mode`` logs for complete visibility into token checks and savings.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agno.compression.manager import CompressionManager
from agno.models.base import Model
from agno.models.message import Message
from agno.models.utils import get_model

if TYPE_CHECKING:
    from ag_ui.core import BaseEvent
    from agno.metrics import RunMetrics

    from .scope import RunScope
    from .types import PostRunHook

logger = logging.getLogger(__name__)

DEFAULT_IN_CONTEXT_CHECKPOINT_PROMPT = """【System Context Compaction Handover】
You are creating an In-Context Checkpoint to compress past conversation and tool execution history while preserving 100% operational continuity. This checkpoint will replace prior turns and serve as your sole working memory for subsequent steps.

Generate a dense, highly structured Markdown document starting immediately with '# CONTEXT CHECKPOINT'. Cover the following sections:

## 1. User Intent & Constraints
- Primary objective, core requirements, and success criteria.
- User preferences, formatting choices (e.g. cards, tables, prose), and explicit constraints.

## 2. Key Decisions & Rationale
- Technical or design decisions made during the conversation and reasons why.
- Agreements or clarifications established across turns.

## 3. Discovered Facts & Ground Truth (CRITICAL)
- Record all concrete data, numerical values, city names, temperatures, file paths, IDs, URLs, and tool output facts.
- CRITICAL: Never generalize or write vague summaries like "weather data was fetched" or "files were checked". Record the EXACT values (e.g. `Tokyo: 25°C, clear; Osaka: 23°C, clear; Sapporo: 19°C, clear; Kyoto: 22°C, clear`).

## 4. Completed Actions & Findings
- Tools invoked and key results obtained.
- Artifacts or files created, modified, read, or deleted.
- Errors, warnings, or unexpected conditions encountered and how they were resolved.

## 5. Active State & Immediate Next Steps
- Exact status of the task right before this checkpoint.
- What was just completed and what remains pending.
- The precise immediate next action to execute to fulfill the user's request.

Rules:
- Maximize information density: use concise bullet points, tables, or key-value pairs.
- Zero fluff: no conversational preamble (e.g. "Sure, here is the summary..."), no meta-commentary, no pleasantries.
- Match the language of the conversation (use Chinese if the conversation is in Chinese, English if English).
- Start directly with '# CONTEXT CHECKPOINT'.
"""

# Lazy cache for tiktoken encoding
_ENCODINGS: dict[str, Any] = {}


def get_tiktoken_encoding(encoding_name: str = "cl100k_base") -> Any:
    """Retrieve or lazily initialize a tiktoken encoding instance."""
    if encoding_name in _ENCODINGS:
        return _ENCODINGS[encoding_name]
    try:
        import tiktoken

        enc = tiktoken.get_encoding(encoding_name)
        _ENCODINGS[encoding_name] = enc
        return enc
    except Exception as exc:
        logger.debug("Failed to initialize tiktoken encoding %s: %s", encoding_name, exc)
        return None


def count_string_tokens(text: str | None, encoding: Any = None) -> int:
    """Count tokens in text with tiktoken, falling back to 4 chars per token."""
    if not text:
        return 0
    if encoding is not None:
        try:
            return len(encoding.encode(str(text)))
        except Exception:
            pass
    return max(1, len(str(text)) // 4)


def count_messages_tokens(
    messages: list[Message] | None,
    encoding_name: str = "cl100k_base",
) -> int:
    """Accurately count total tokens across a list of Messages."""
    if not messages:
        return 0
    encoding = get_tiktoken_encoding(encoding_name)
    total_tokens = 3  # every reply is primed with <|im_start|>assistant<|im_sep|>
    for msg in messages:
        total_tokens += 4  # message overhead: <|im_start|>{role}\n{content}<|im_end|>\n
        if msg.role:
            total_tokens += count_string_tokens(msg.role, encoding)
        if msg.content:
            total_tokens += count_string_tokens(str(msg.content), encoding)
        if msg.name:
            total_tokens += count_string_tokens(msg.name, encoding)
        if msg.tool_call_id:
            total_tokens += count_string_tokens(msg.tool_call_id, encoding)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
                fn_name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", "")
                fn_args = (
                    fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", "")
                )
                total_tokens += count_string_tokens(str(fn_name), encoding)
                total_tokens += count_string_tokens(str(fn_args), encoding)
    return total_tokens


def count_tools_tokens(
    tools: list[Any] | None,
    encoding_name: str = "cl100k_base",
) -> int:
    """Count token overhead of tool schema definitions sent to the model."""
    if not tools:
        return 0
    encoding = get_tiktoken_encoding(encoding_name)
    total_tokens = 0
    for t in tools:
        if isinstance(t, dict):
            total_tokens += count_string_tokens(json.dumps(t, ensure_ascii=False), encoding)
        elif hasattr(t, "to_dict") and callable(t.to_dict):
            try:
                total_tokens += count_string_tokens(
                    json.dumps(t.to_dict(), ensure_ascii=False), encoding
                )
            except Exception:
                total_tokens += count_string_tokens(str(t), encoding)
        elif hasattr(t, "__dict__"):
            total_tokens += count_string_tokens(str(t.__dict__), encoding)
        else:
            total_tokens += count_string_tokens(str(t), encoding)
    return total_tokens


def reorganize_messages_with_checkpoint(
    messages: list[Message],
    checkpoint_text: str,
) -> list[Message]:
    """Reorganize in-flight messages into a compact context with Checkpoint.

    Preserves:
    1. System prompt (instructions) at messages[0]
    2. # CONTEXT CHECKPOINT as a user message + assistant acknowledgment
    3. Active turn's user message
    4. Active turn's latest tool call and tool result(s)
    """
    checkpoint_user = Message(
        role="user",
        content=f"[Context Checkpoint]\n{checkpoint_text}",
    )
    checkpoint_asst = Message(
        role="assistant",
        content="Understood. I have recorded the context checkpoint.",
    )

    if not messages:
        return [checkpoint_user, checkpoint_asst]

    system_msg = messages[0] if messages[0].role == "system" else None

    # Find the active user message index (the most recent user message)
    active_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            active_user_idx = i
            break

    # Find the last assistant message that made tool calls in the CURRENT active turn
    # (strictly after active_user_idx). Previous turns' tool calls are already summarized
    # into the checkpoint and must never trail after the current user message.
    last_tool_call_idx = None
    if active_user_idx is not None:
        for i in range(len(messages) - 1, active_user_idx, -1):
            if messages[i].role == "assistant" and messages[i].tool_calls:
                last_tool_call_idx = i
                break

    new_messages: list[Message] = []
    if system_msg is not None:
        new_messages.append(system_msg)

    new_messages.append(checkpoint_user)
    new_messages.append(checkpoint_asst)

    if active_user_idx is not None:
        active_user = messages[active_user_idx]
        if system_msg is None or active_user != system_msg:
            new_messages.append(active_user)

        # Only append in-flight tool calls/results if they belong to the CURRENT turn
        if last_tool_call_idx is not None:
            for m in messages[last_tool_call_idx:]:
                if m not in new_messages:
                    new_messages.append(m)

    return new_messages


def structural_prune(content: str | Any, max_chars: int = 1500) -> str:
    """Helper for JSON/text truncation. Preserves outlines and previews."""
    if not content:
        return ""
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    if len(text) <= max_chars:
        return text

    # Try parsing as JSON to preserve top keys / sample items
    try:
        data = json.loads(text)
        if isinstance(data, list):
            sample = data[:3]
            summary = {
                "total_items": len(data),
                "preview_first_3": sample,
                "note": f"Truncated {len(data) - 3} remaining items.",
            }
            res = json.dumps(summary, ensure_ascii=False)
            if len(res) <= max_chars:
                return res
            minimal = {
                "total_items": len(data),
                "preview_first_3": [str(x)[:40] for x in sample],
                "note": f"Truncated {len(data) - 3} items.",
            }
            min_res = json.dumps(minimal, ensure_ascii=False)
            if len(min_res) <= max_chars:
                return min_res
        elif isinstance(data, dict):
            condensed: dict[str, Any] = {}
            for k, v in data.items():
                if isinstance(v, (str, bytes)) and len(str(v)) > 300:
                    condensed[k] = str(v)[:300] + "... [truncated]"
                elif isinstance(v, list) and len(v) > 3:
                    condensed[k] = v[:3] + [f"... and {len(v) - 3} more items"]
                else:
                    condensed[k] = v
            res = json.dumps(condensed, ensure_ascii=False)
            if len(res) <= max_chars:
                return res
    except Exception:
        pass

    indicator = "\n... [Omitted context] ...\n"
    if max_chars <= len(indicator):
        return text[:max_chars]
    available = max_chars - len(indicator)
    head_len = int(available * 0.7)
    tail_len = available - head_len
    return text[:head_len] + indicator + (text[-tail_len:] if tail_len > 0 else "")


@dataclass
class SmartCompressionManager(CompressionManager):
    """Enhanced CompressionManager using In-Context Checkpointing and tiktoken budgeting."""

    model: Model | None = None
    compress_tool_results: bool = True
    trigger_token_limit: int = 12000
    min_messages: int = 4
    checkpoint_instructions: str | None = None
    debug_mode: bool = False
    encoding_name: str = "cl100k_base"
    compress_tool_results_limit: int | None = 3
    max_single_tool_chars: int = 1500
    use_llm: bool = True
    llm_timeout: float = 15.0
    last_checkpoint: dict[str, Any] | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    def _log(self, msg: str, *args: Any) -> None:
        formatted = msg % args if args else msg
        try:
            from agno.utils.log import log_info

            log_info(formatted)
        except Exception:
            logger.info("%s", formatted)

    def should_compress(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        model: Model | None = None,
        response_format: Any = None,
    ) -> bool:
        """Check if messages exceed trigger token limit using tiktoken."""
        if not self.compress_tool_results:
            if self.debug_mode:
                self._log(
                    "[SmartCompression] 🔍 Check: skipped (compress_tool_results is disabled)"
                )
            return False

        msg_count = len(messages) if messages else 0
        messages_tokens = count_messages_tokens(messages, self.encoding_name) if messages else 0
        tools_tokens = count_tools_tokens(tools, self.encoding_name)
        total_tokens = messages_tokens + tools_tokens

        if msg_count < self.min_messages:
            if self.debug_mode:
                self._log(
                    "[SmartCompression] 🔍 Pre-LLM Check: %d msgs (< min_messages %d), %d tokens (msgs: %d, tools: %d) -> SKIP",
                    msg_count,
                    self.min_messages,
                    total_tokens,
                    messages_tokens,
                    tools_tokens,
                )
            return False

        should = total_tokens >= self.trigger_token_limit

        if self.debug_mode:
            status = (
                f"⚡ TRIGGERED ({total_tokens:,} >= {self.trigger_token_limit:,})"
                if should
                else f"SKIP ({total_tokens:,} < {self.trigger_token_limit:,})"
            )
            tools_info = (
                f" (msgs: {messages_tokens:,} + tools: {tools_tokens:,})"
                if tools_tokens > 0
                else ""
            )
            self._log(
                "[SmartCompression] 🔍 Pre-LLM Check: %d msgs, %d total tokens%s, trigger limit: %d -> %s",
                msg_count,
                total_tokens,
                tools_info,
                self.trigger_token_limit,
                status,
            )

        return should

    async def ashould_compress(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        model: Model | None = None,
        response_format: Any = None,
    ) -> bool:
        """Async variant of should_compress."""
        return self.should_compress(messages, tools, model, response_format)

    def _compress_tool_result(
        self,
        tool_result: Message,
        run_metrics: RunMetrics | None = None,
    ) -> str | None:
        """Do NOT compress individual tools separately. Return content as-is."""
        return str(tool_result.content or "") if tool_result else None

    async def _acompress_tool_result(
        self,
        tool_result: Message,
        run_metrics: RunMetrics | None = None,
    ) -> str | None:
        """Do NOT compress individual tools separately. Return content as-is."""
        return str(tool_result.content or "") if tool_result else None

    def compress(
        self,
        messages: list[Message],
        run_metrics: RunMetrics | None = None,
    ) -> None:
        """Sync compress not supported for in-context async checkpointing."""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already in async loop
                asyncio.create_task(self.acompress(messages, run_metrics=run_metrics))
                return
            loop.run_until_complete(self.acompress(messages, run_metrics=run_metrics))
        except Exception as e:
            logger.warning("[SmartCompression] Sync compress fallback failed: %s", e)

    async def acompress(
        self,
        messages: list[Message],
        run_metrics: RunMetrics | None = None,
    ) -> None:
        """In-Context Checkpoint compression.

        Preserves 100% of the KV cache prefix by appending the checkpoint prompt
        to the existing message stream, letting the agent summarize itself, and
        then reorganizing the message list in place.
        """
        if not self.compress_tool_results or not messages:
            return

        # Start OTLP Span for tracing if opentelemetry is available
        span_cm: Any = None
        span: Any = None
        try:
            otel_trace = importlib.import_module("opentelemetry.trace")
            tracer = otel_trace.get_tracer("better-agno-toolbox.compression")
            span_cm = tracer.start_as_current_span("context_compression")
            span = span_cm.__enter__()
        except Exception:
            span_cm = None
            span = None

        try:
            orig_tokens = count_messages_tokens(messages, self.encoding_name)
            orig_msg_count = len(messages)

            if span is not None and hasattr(span, "set_attribute"):
                span.set_attribute("compression.original_tokens", orig_tokens)
                span.set_attribute("compression.messages_before", orig_msg_count)

            if self.debug_mode:
                self._log(
                    "[SmartCompression] ⚡ Starting In-Context Checkpoint compression on conversation history (msgs: %d tokens, %d messages)...",
                    orig_tokens,
                    orig_msg_count,
                )

            model_instance = (
                self.model if hasattr(self.model, "aresponse") else get_model(self.model)
            )
            if not model_instance:
                self._log(
                    "[SmartCompression] ⚠️ No model available on SmartCompressionManager; skipping compression."
                )
                return

            prompt = self.checkpoint_instructions or DEFAULT_IN_CONTEXT_CHECKPOINT_PROMPT
            checkpoint_request_messages = list(messages) + [
                Message(role="user", content=prompt),
            ]

            t0 = time.perf_counter()
            response = await model_instance.aresponse(messages=checkpoint_request_messages)
            elapsed = time.perf_counter() - t0

            checkpoint_text = response.content or ""
            if not checkpoint_text:
                self._log("[SmartCompression] ⚠️ Checkpoint LLM returned empty response.")
                return

            if run_metrics is not None:
                from agno.metrics import ModelType, accumulate_model_metrics

                accumulate_model_metrics(
                    response, model_instance, ModelType.COMPRESSION_MODEL, run_metrics
                )

            # Reorganize messages in-place
            new_messages = reorganize_messages_with_checkpoint(messages, checkpoint_text)
            messages[:] = new_messages

            compacted_tokens = count_messages_tokens(messages, self.encoding_name)
            saved_tokens = max(0, orig_tokens - compacted_tokens)

            cp_data = {
                "id": f"cp_{int(time.time() * 1000)}",
                "content": checkpoint_text,
                "original_tokens": orig_tokens,
                "compacted_tokens": compacted_tokens,
                "saved_tokens": saved_tokens,
                "created_at": time.time(),
            }
            self.last_checkpoint = cp_data
            self.stats["original_size"] = orig_tokens
            self.stats["compressed_size"] = compacted_tokens
            self.stats["saved_tokens"] = saved_tokens
            self.stats["checkpoint"] = checkpoint_text
            self.stats["checkpoints_generated"] = self.stats.get("checkpoints_generated", 0) + 1
            self.stats["tool_results_compressed"] = 1

            if span is not None and hasattr(span, "set_attribute"):
                span.set_attribute("compression.compacted_tokens", compacted_tokens)
                span.set_attribute("compression.saved_tokens", saved_tokens)
                span.set_attribute("compression.messages_after", len(messages))
                span.set_attribute("compression.elapsed_seconds", elapsed)

            if self.debug_mode:
                self._log(
                    "[SmartCompression] ✅ Checkpoint generated in %.2fs. Conversation history: %d -> %d tokens (saved %d tokens, %.1f%%). Messages: %d -> %d.",
                    elapsed,
                    orig_tokens,
                    compacted_tokens,
                    saved_tokens,
                    (saved_tokens / orig_tokens * 100) if orig_tokens else 0,
                    orig_msg_count,
                    len(messages),
                )
        finally:
            if span_cm is not None:
                with contextlib.suppress(Exception):
                    span_cm.__exit__(None, None, None)


def make_checkpoint_hook(
    runtime: Any, manager: SmartCompressionManager | None = None
) -> PostRunHook:
    """Post-run hook to persist in-context checkpoints to session metadata.

    When SmartCompressionManager produces a checkpoint during a run, this hook
    attaches it to the run metadata in the session database so future turns can
    prune older history and hit KV cache efficiently.
    """

    async def checkpoint_hook(
        scope: RunScope, *, completion: Any, error: BaseException | None
    ) -> AsyncIterator[BaseEvent]:
        if error is not None:
            return

        mgr = manager
        if mgr is None:
            agent = getattr(runtime, "agent", None)
            candidate = getattr(agent, "compression_manager", None)
            if isinstance(candidate, SmartCompressionManager):
                mgr = candidate

        if mgr is None or not mgr.last_checkpoint:
            return

        try:
            from .closure import attach_checkpoint_to_session

            await attach_checkpoint_to_session(
                runtime.db,
                session_id=scope.thread_id,
                run_id=scope.run_id,
                checkpoint_data=mgr.last_checkpoint,
            )
            logger.info(
                "Persisted context checkpoint to session %s for run %s",
                scope.thread_id,
                scope.run_id,
            )
            mgr.last_checkpoint = None
        except Exception:
            logger.exception("Failed to attach context checkpoint to session %s", scope.thread_id)

        if False:
            yield

    return checkpoint_hook


__all__ = [
    "DEFAULT_IN_CONTEXT_CHECKPOINT_PROMPT",
    "SmartCompressionManager",
    "count_messages_tokens",
    "count_string_tokens",
    "count_tools_tokens",
    "get_tiktoken_encoding",
    "make_checkpoint_hook",
    "reorganize_messages_with_checkpoint",
    "structural_prune",
]
