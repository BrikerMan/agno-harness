"""Tool closure and session sealing for interrupted / cancelled / errored runs.

When an agent run is interrupted mid-execution (user cancelled or step failed),
unclosed tool calls left dangling cause LLM providers to reject the next turn
with 400 Bad Request. Moreover, Agno's default ``get_messages()`` drops any run
whose status is CANCELLED or ERROR, losing valuable intermediate progress.

This module provides:
1. ``close_dangling_tool_calls``: synthesizes closing tool messages for unclosed
   tool calls with clean, sanitized status payloads.
2. ``seal_session_run``: seals an interrupted run in the session database,
   closing any dangling tool calls and tagging ``metadata["sealed"] = True``
   while preserving the real status (CANCELLED / ERROR) for auditing.
3. ``install_sealed_history_hook``: enables ``AgentSession.get_messages()`` to
   include sealed runs in conversation history for the next turn.

Usage:
    from agno_relay.runtime.closure import (
        close_dangling_tool_calls,
        seal_session_run,
        install_sealed_history_hook,
    )
    install_sealed_history_hook()
    await seal_session_run(db, session_id, run_id, reason="User cancelled")
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any

from agno.models.message import Message
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False


def sanitize_reason(reason: str | None, max_len: int = 300) -> str:
    """Sanitize and truncate error/cancellation reason to avoid context bloat."""
    if not reason:
        return "Interrupted"
    clean = reason.strip()
    if "Traceback (most recent call last):" in clean:
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        if lines:
            clean = lines[-1]
    if len(clean) > max_len:
        clean = clean[:max_len] + "..."
    return clean


def close_dangling_tool_calls(
    messages: list[Message] | None,
    *,
    reason: str = "Interrupted by user",
    is_error: bool = False,
) -> list[Message]:
    """Ensure all assistant tool calls have matching tool messages.

    Scans the conversation messages for any ``tool_calls`` emitted by an
    assistant that were not followed by a matching ``role="tool"`` message.
    For each dangling tool call, a synthetic tool response is appended so that
    subsequent LLM requests strictly satisfy provider protocol constraints.
    """
    if not messages:
        return []

    sealed = list(messages)
    pending_tool_calls: dict[str, dict[str, Any]] = {}

    for msg in sealed:
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
                fn_name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", "tool")
                if call_id:
                    pending_tool_calls[call_id] = {"name": fn_name, "id": call_id}
        elif msg.role == "tool" and msg.tool_call_id:
            pending_tool_calls.pop(msg.tool_call_id, None)

    if not pending_tool_calls:
        return sealed

    clean_reason = sanitize_reason(reason)

    for call_id, info in pending_tool_calls.items():
        fn_name = info.get("name") or "tool"
        if fn_name in ("ask_user", "ask_user_theme"):
            payload = {
                "status": "skipped",
                "note": clean_reason or "User skipped this question and provided a new instruction",
            }
        else:
            payload = {
                "status": "error" if is_error else "interrupted",
                "note": clean_reason,
            }

        synth_msg = Message(
            role="tool",
            tool_call_id=call_id,
            tool_name=fn_name,
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_error=is_error,
        )
        sealed.append(synth_msg)

    return sealed


async def _get_db_session(db: Any, session_id: str) -> AgentSession | None:
    """Read a session from db, handling both sync and async DB instances."""
    if db is None:
        return None
    res = db.get_session(session_id=session_id)
    if inspect.isawaitable(res):
        return await res
    return res


async def _upsert_db_session(db: Any, session: AgentSession) -> Any:
    """Upsert a session to db, handling both sync and async DB instances."""
    if db is None:
        return None
    res = db.upsert_session(session)
    if inspect.isawaitable(res):
        return await res
    return res


async def seal_session_run(
    db: Any,
    session_id: str,
    run_id: str | None = None,
    *,
    reason: str = "Interrupted by user",
    is_error: bool = False,
    extra_metadata: dict[str, Any] | None = None,
    partial_content: str | None = None,
) -> bool:
    """Seal an interrupted run in the session database.

    Synthesizes closing messages for any dangling tool calls in the run's
    message history, attaches any partial streamed output to the assistant message,
    and marks ``run.metadata["sealed"] = True``. The true run status (CANCELLED / ERROR)
    is preserved for reporting and audit integrity.
    """
    if db is None or not session_id:
        return False

    session = await _get_db_session(db, session_id)
    if not session or not session.runs:
        return False

    target_run = None
    if run_id:
        target_run = next((r for r in session.runs if getattr(r, "run_id", None) == run_id), None)
    if target_run is None:
        target_run = session.runs[-1]

    if getattr(target_run, "metadata", None) is None:
        target_run.metadata = {}

    if target_run.metadata is not None:
        target_run.metadata["sealed"] = True
        target_run.metadata["interrupted"] = True
        target_run.metadata["interruption_reason"] = sanitize_reason(reason)
        target_run.metadata["is_error"] = is_error
        if extra_metadata:
            target_run.metadata.update(extra_metadata)

    # Sync partial content to target_run.content if newer or missing
    current_content = getattr(target_run, "content", None) or ""
    if partial_content and (not current_content or len(partial_content) > len(current_content)):
        target_run.content = partial_content
        current_content = partial_content

    effective_content = current_content or partial_content or ""

    if target_run.messages is None:
        target_run.messages = []

    # 1. Close dangling tool calls
    target_run.messages = close_dangling_tool_calls(
        target_run.messages,
        reason=reason,
        is_error=is_error,
    )

    # 2. Ensure the partial output is preserved as an assistant message in run.messages
    # Agno's _handle_run_cancellation skips attaching partial content if run_response.messages
    # was already initialized, leaving the assistant turn completely missing from history.
    msgs = list(target_run.messages)
    has_assistant = any(getattr(m, "role", None) == "assistant" for m in msgs)

    if not has_assistant:
        synth_content = effective_content if effective_content else f"[{sanitize_reason(reason)}]"
        msgs.append(
            Message(
                role="assistant",
                content=synth_content,
                add_to_agent_memory=True,
            )
        )
        target_run.messages = msgs
    else:
        last_asst = next(
            (m for m in reversed(msgs) if getattr(m, "role", None) == "assistant"), None
        )
        if last_asst:
            current_c = getattr(last_asst, "content", None)
            if (not current_c or current_c == "None") and effective_content:
                last_asst.content = effective_content
        # If the last message is a tool, but a subsequent partial text answer was streamed
        if (
            partial_content
            and msgs
            and getattr(msgs[-1], "role", None) == "tool"
            and last_asst
            and partial_content != getattr(last_asst, "content", None)
        ):
            msgs.append(
                Message(
                    role="assistant",
                    content=partial_content,
                    add_to_agent_memory=True,
                )
            )
            target_run.messages = msgs

    tools = getattr(target_run, "tools", None)
    if tools:
        for tool in tools:
            if getattr(tool, "result", None) is None:
                tool.tool_call_error = True
                tool.result = json.dumps(
                    {
                        "status": "error" if is_error else "interrupted",
                        "note": sanitize_reason(reason),
                    },
                    ensure_ascii=False,
                )

    await _upsert_db_session(db, session)
    logger.info(
        "Successfully sealed run %s in session %s (reason: %s, content_len: %d)",
        getattr(target_run, "run_id", None),
        session_id,
        reason,
        len(effective_content),
    )
    return True


def strip_stream_ui(text: str | None) -> str:
    """Strip <stream-ui> tags and component chrome while preserving textual/artifact bodies.

    For document cards (artifacts, presentation decks, code listings, diffs), the inner
    text/markdown content is preserved with a clean descriptive bracket header.
    For structured item widgets (weather, movie lists, etc.), the JSON data rows are stripped
    to keep context compact.
    Unclosed blocks from interrupted runs retain their partial content with an '(interrupted)' note.
    """
    if not text:
        return ""

    def _format_block(header_raw: str, body_raw: str, closed: bool) -> str:
        stripped_body = body_raw.strip()
        if (
            not stripped_body
            or stripped_body.startswith("<component")
            or stripped_body.startswith("<stream-ui")
        ):
            return ""

        schema = ""
        title = ""
        path = ""
        schema_m = re.search(r'(?:schema|component)=["\']([^"\']+)["\']', header_raw)
        if schema_m:
            schema = schema_m.group(1)
        title_m = re.search(r'title=["\']([^"\']+)["\']', header_raw)
        if title_m:
            title = title_m.group(1)
        path_m = re.search(r'(?:path|filepath)=["\']([^"\']+)["\']', header_raw)
        if path_m:
            path = path_m.group(1)

        lines = stripped_body.splitlines()
        first_line = lines[0].strip() if lines else ""
        body_text = stripped_body

        if first_line.startswith("{") and first_line.endswith("}"):
            try:
                hdr = json.loads(first_line)
                if isinstance(hdr, dict):
                    schema = schema or hdr.get("schema", "")
                    title = title or hdr.get("title", "")
                    path = path or hdr.get("path", "") or hdr.get("filepath", "")
                    body_text = "\n".join(lines[1:]).strip()
            except Exception:
                pass

        text_schemas = {"artifact", "presentation_deck", "code-card", "diff"}
        is_text_body = schema in text_schemas or (
            body_text and not body_text.startswith("{") and not body_text.startswith("<")
        )

        if is_text_body and body_text:
            status_suffix = " (interrupted)" if not closed else ""
            label_parts = [schema or "Document"]
            if title:
                label_parts.append(f'title="{title}"')
            if path:
                label_parts.append(f'path="{path}"')
            header = "[" + " ".join(label_parts) + status_suffix + "]"
            return f"\n{header}\n{body_text}\n"

        if schema:
            return f"\n[{schema}]\n"
        return ""

    # 1. Closed tags: <stream-ui ...> ... </stream-ui>
    p_closed = re.compile(r"(<stream-ui(?:\s+[^>]*)?>)([\s\S]*?)(</stream-ui>)", re.DOTALL)
    res = p_closed.sub(lambda m: _format_block(m.group(1), m.group(2), closed=True), text)

    # 2. Closed markdown fences: ```stream-ui ... ```
    p_fence_closed = re.compile(r"(```(?:stream-ui)?[^\n]*\n)([\s\S]*?)(```)", re.DOTALL)

    def _fence_repl(m: re.Match) -> str:
        if "stream-ui" in m.group(1):
            return _format_block(m.group(1), m.group(2), closed=True)
        return m.group(0)

    res = p_fence_closed.sub(_fence_repl, res)

    # 3. Unclosed tag at end: <stream-ui ...> ...
    p_unclosed = re.compile(r"(<stream-ui(?:\s+[^>]*)?>)([\s\S]*)$", re.DOTALL)
    res = p_unclosed.sub(lambda m: _format_block(m.group(1), m.group(2), closed=False), res)

    # 4. Unclosed markdown fence at end: ```stream-ui ...
    p_fence_unclosed = re.compile(r"(```stream-ui[^\n]*\n)([\s\S]*)$", re.DOTALL)
    res = p_fence_unclosed.sub(lambda m: _format_block(m.group(1), m.group(2), closed=False), res)

    return res.strip()


async def attach_checkpoint_to_session(
    db: Any,
    session_id: str,
    run_id: str | None = None,
    checkpoint_data: dict[str, Any] | None = None,
) -> bool:
    """Attach a context checkpoint to a run in session storage.

    Allows subsequent chat turns to skip all runs prior to this checkpoint,
    providing high token savings and KV cache optimization across multi-turn sessions.
    """
    if db is None or not session_id or not checkpoint_data:
        return False

    session = await _get_db_session(db, session_id)
    if not session or not session.runs:
        return False

    target_run = None
    if run_id:
        target_run = next((r for r in session.runs if getattr(r, "run_id", None) == run_id), None)
    if target_run is None:
        target_run = session.runs[-1]

    if getattr(target_run, "metadata", None) is None:
        target_run.metadata = {}

    if target_run.metadata is not None:
        target_run.metadata["checkpoint"] = checkpoint_data

    if getattr(session, "session_data", None) is None:
        session.session_data = {}
    if session.session_data is not None:
        session.session_data["latest_checkpoint"] = checkpoint_data
        session.session_data["latest_checkpoint_run_id"] = getattr(target_run, "run_id", None)

    await _upsert_db_session(db, session)
    logger.info(
        "Attached checkpoint to run %s in session %s (%d chars)",
        getattr(target_run, "run_id", None),
        session_id,
        len(checkpoint_data.get("content", "")),
    )
    return True


def install_sealed_history_hook() -> None:
    """Install the sealed history hook on ``AgentSession.get_messages``.

    Allows runs marked with ``metadata["sealed"] = True`` to be included
    in historical messages without modifying their persisted CANCELLED/ERROR
    status in the database.
    Also handles context checkpoints: if a run contains a checkpoint in its
    metadata, all prior runs are pruned from history, injecting the checkpoint
    as a system message and keeping only the checkpoint run's query/final answer
    plus subsequent runs.
    """
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return

    original_get_messages = AgentSession.get_messages

    def _sealed_aware_get_messages(self: AgentSession, *args: Any, **kwargs: Any) -> list[Message]:
        runs = self.runs or []
        temp_restores: list[tuple[Any, Any]] = []
        temp_msg_restores: list[tuple[Any, Any]] = []

        for r in runs:
            status = getattr(r, "status", None)
            meta = getattr(r, "metadata", None) or {}
            is_sealed = bool(meta.get("sealed") or meta.get("interrupted"))
            if status in (RunStatus.cancelled, RunStatus.error) and is_sealed:
                temp_restores.append((r, status))
                r.status = RunStatus.completed

            # In-memory repair for runs where assistant partial output was saved in r.content
            # but omitted from r.messages due to cancellation/stream interruption.
            if is_sealed or status == RunStatus.completed:
                msgs = list(getattr(r, "messages", None) or [])
                content_val = getattr(r, "content", None) or ""
                has_assistant = any(getattr(m, "role", None) == "assistant" for m in msgs)
                if not has_assistant and content_val:
                    temp_msg_restores.append((r, r.messages))
                    msgs.append(
                        Message(role="assistant", content=content_val, add_to_agent_memory=True)
                    )
                    r.messages = msgs
                elif msgs and getattr(msgs[-1], "role", None) == "assistant":
                    last_c = getattr(msgs[-1], "content", None)
                    if (not last_c or last_c == "None") and content_val:
                        temp_msg_restores.append((r, list(r.messages or [])))
                        msgs[-1].content = content_val
                        r.messages = msgs

        try:
            # Check if any run has a checkpoint
            checkpoint_idx = -1
            for i in range(len(runs) - 1, -1, -1):
                meta = getattr(runs[i], "metadata", None) or {}
                if meta.get("checkpoint"):
                    checkpoint_idx = i
                    break

            if checkpoint_idx == -1:
                # No checkpoint: return standard history with stream-ui cleaned
                raw_messages = original_get_messages(self, *args, **kwargs)
                for m in raw_messages:
                    if getattr(m, "role", None) == "assistant" and isinstance(m.content, str):
                        cleaned = strip_stream_ui(m.content)
                        m.content = cleaned if cleaned else "[Rendered UI Component]"
                return raw_messages

            # Checkpoint exists! Prune all runs prior to checkpoint_idx
            cp_run = runs[checkpoint_idx]
            cp_meta = (getattr(cp_run, "metadata", None) or {}).get("checkpoint")
            cp_content = cp_meta.get("content", "") if isinstance(cp_meta, dict) else str(cp_meta)

            result_messages: list[Message] = []
            # 1. Checkpoint as user message + assistant acknowledgment
            # Keeps history free of system messages so OpenAI-compatible providers (vLLM, Qwen, etc.)
            # requiring system message strictly at prompt start do not throw 400.
            result_messages.append(
                Message(
                    role="user",
                    content=f"[Context Checkpoint]\n{cp_content}",
                    name="context_checkpoint",
                    from_history=True,
                )
            )
            result_messages.append(
                Message(
                    role="assistant",
                    content="Understood. I have recorded the context checkpoint.",
                    name="context_checkpoint_ack",
                    from_history=True,
                )
            )

            # 2. Checkpoint run's user prompt and final assistant response
            cp_run_msgs = getattr(cp_run, "messages", None) or []
            user_msg = next((m for m in cp_run_msgs if getattr(m, "role", None) == "user"), None)
            if user_msg:
                result_messages.append(
                    Message(
                        role="user",
                        content=user_msg.content,
                        from_history=True,
                    )
                )

            final_asst = next(
                (m for m in reversed(cp_run_msgs) if getattr(m, "role", None) == "assistant"), None
            )
            if final_asst:
                cleaned_asst = strip_stream_ui(final_asst.content)
                result_messages.append(
                    Message(
                        role="assistant",
                        content=cleaned_asst if cleaned_asst else "[Rendered UI Component]",
                        from_history=True,
                    )
                )

            # 3. Subsequent runs after the checkpoint run
            subsequent_runs = runs[checkpoint_idx + 1 :]
            if subsequent_runs:
                old_runs = self.runs
                self.runs = subsequent_runs
                try:
                    # After a checkpoint, subsequent runs are all active until next checkpoint;
                    # do not truncate them with last_n_runs.
                    clean_kwargs = dict(kwargs)
                    clean_kwargs["last_n_runs"] = None
                    subsequent_msgs = original_get_messages(self, *args, **clean_kwargs)
                finally:
                    self.runs = old_runs

                for m in subsequent_msgs:
                    if getattr(m, "role", None) == "assistant" and isinstance(m.content, str):
                        cleaned = strip_stream_ui(m.content)
                        m.content = cleaned if cleaned else "[Rendered UI Component]"
                result_messages.extend(subsequent_msgs)

            return result_messages
        finally:
            for r, old_status in temp_restores:
                r.status = old_status
            for r, old_msgs in temp_msg_restores:
                r.messages = old_msgs

    AgentSession.get_messages = _sealed_aware_get_messages  # type: ignore[assignment]
    _HOOK_INSTALLED = True
    logger.debug("Installed sealed & checkpoint-aware history hook on AgentSession.get_messages")
