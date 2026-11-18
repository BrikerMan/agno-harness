"""Generic sub-agent delegation toolkit.

Instead of hand-writing ``delegate_to_reviewer`` / ``delegate_to_researcher`` for
every child agent, register them once::

    tools = [SubAgentToolkit(agents=[reviewer, researcher])]

The parent gets a single tool — ``delegate_subagent`` — and the roster is
injected into its system prompt via Agno's ``add_instructions=True``, so editing
a sub-agent's ``description`` updates every parent that can call it.

By default the nested run is streamed through :func:`substream`, so the user
watches the sub-agent work live inside a panel. Pass ``stream=False`` for the
    blocking ``await arun(...)`` behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agno.agent import Agent
from agno.run.base import RunContext
from agno.tools import Toolkit

from agno_harness.runtime.closure import (
    clear_unfinished_task,
    get_unfinished_task,
    install_sealed_history_hook,
    seal_session_run,
)
from agno_harness.runtime.modules.subagent import substream

logger = logging.getLogger(__name__)

TOOL_NAME = "delegate_subagent"

_STREAMUI_OPEN_RE = re.compile(r"^\s*(?:<stream-ui\b|(?::{3,}|`{3,})stream-ui\b)", re.IGNORECASE)
_STREAMUI_CLOSE_RE = re.compile(r"^\s*(?:</stream-ui\s*>|:{3,}\s*$|`{3,}\s*$)", re.IGNORECASE)
_PARTIAL_META_PREFIX = "<!-- agno_partial_meta: "
_PARTIAL_META_SUFFIX = " -->\n"


def safe_cut(text: str) -> str:
    """Drop a trailing unclosed stream-ui block; everything else is kept as shown.

    The user already saw the text, so a continuation must append to exactly that
    (mid-sentence is fine). Only a half-written card cannot be resumed reliably,
    so it is dropped and written again in full.
    """
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    open_at: int | None = None
    offset = 0
    for line in lines:
        if open_at is None and _STREAMUI_OPEN_RE.match(line):
            open_at = offset
        elif open_at is not None and _STREAMUI_CLOSE_RE.match(line):
            open_at = None
        offset += len(line)
    if open_at is None:
        return text
    return text[:open_at].rstrip() + "\n\n" if text[:open_at].strip() else ""


def build_continuation_prompt(
    task_prompt: str,
    written: str,
    tail_chars: int = 6000,
    *,
    dropped_block: bool = False,
    resume_instruction: str | None = None,
) -> str:
    """Build a self-healing continuation prompt that preserves the original task and written tail."""
    tail = written[-tail_chars:] if len(written) > tail_chars else written
    ending = (
        "An unfinished stream-ui card at the end was dropped: start by writing that card again in full, then continue."
        if dropped_block
        else "It ends exactly where the stream stopped, possibly mid-sentence: continue from the very next character."
    )
    sections = [
        "[Continuation] Your previous attempt at this task stopped before the deliverable was produced.",
    ]
    if written:
        sections.append(
            "Everything below 'ALREADY WRITTEN' has already been shown to the user.\n"
            f"{ending} Do not repeat it, do not restart sections, no preamble. "
            "If your deliverable file was already saved, only finish the remaining reply text."
        )
    sections.append(
        "CRITICAL: Strictly use the exact target entities, scope, and constraints from ORIGINAL TASK "
        "and your prior tool call history. Never substitute, guess, or fetch unrelated top-ranking items "
        "if an earlier output or file was truncated."
    )
    sections.append(f"ORIGINAL TASK:\n{task_prompt}")
    if (
        resume_instruction
        and resume_instruction.strip()
        and resume_instruction.strip() != task_prompt.strip()
    ):
        sections.append(f"FOLLOW-UP / RESUME INSTRUCTION:\n{resume_instruction.strip()}")
    if written:
        sections.append(
            f"ALREADY WRITTEN (tail, {len(tail)} of {len(written)} chars):\n{tail}\n"
            "--- continue right after this line ---"
        )
    return "\n\n".join(sections)


def save_partial_state(
    partial_path: Path | None,
    *,
    original_prompt: str,
    written: str,
) -> None:
    """Persist both the original task prompt and partial written text to a workspace partial file."""
    if partial_path is None or (not original_prompt and not written):
        return
    meta_json = json.dumps({"original_prompt": original_prompt}, ensure_ascii=False)
    payload = f"{_PARTIAL_META_PREFIX}{meta_json}{_PARTIAL_META_SUFFIX}{written}"
    with contextlib.suppress(OSError):
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path.write_text(payload, encoding="utf-8")


def load_partial_state(partial_path: Path | None) -> tuple[str, str]:
    """Load ``(original_prompt, safe_cut(written))`` from a workspace partial file."""
    if partial_path is None or not partial_path.exists():
        return "", ""
    try:
        raw = partial_path.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    if raw.startswith(_PARTIAL_META_PREFIX):
        end_idx = raw.find(_PARTIAL_META_SUFFIX)
        if end_idx != -1:
            meta_str = raw[len(_PARTIAL_META_PREFIX) : end_idx]
            body = raw[end_idx + len(_PARTIAL_META_SUFFIX) :]
            try:
                meta = json.loads(meta_str)
                orig = str(meta.get("original_prompt") or "")
                return orig, safe_cut(body)
            except Exception:
                return "", safe_cut(body)
    return "", safe_cut(raw)


def clear_partial_state(partial_path: Path | None) -> None:
    """Remove a workspace partial state file if it exists."""
    if partial_path is not None and partial_path.exists():
        with contextlib.suppress(OSError):
            partial_path.unlink()


class SubAgentToolkit(Toolkit):
    """One tool that can reach any of the registered sub-agents.

    Parameters
    ----------
    agents / sub_agents:
        Agno agents to expose. Lookup key is ``agent.name`` — names must be
        unique within the toolkit.
    stream:
        When True (default), forward the nested run through ``substream`` so
        AG-UI clients render a live sub-agent panel. When False, await a full
        ``arun`` and return only the final text (cheaper on the wire, opaque
        to the user while it runs).
    hide_from_parent_card:
        Informational flag. The toolkit itself does not register a filter —
        callers normally pass ``HideToolFilter({SubAgentToolkit.TOOL_NAME})``
        (or ``toolkit.tool_names``) so the panel is not duplicated by a tool
        card. Exposed here so the intent is discoverable next to construction.
    """

    TOOL_NAME = TOOL_NAME

    DEFAULT_FIRST_CHUNK_TIMEOUT: float = 75.0
    DEFAULT_INTER_CHUNK_TIMEOUT: float = 120.0
    DEFAULT_MAX_ATTEMPTS: int = 2
    CONTINUATION_TAIL_CHARS: int = 6000

    def __init__(
        self,
        agents: Sequence[Agent] | None = None,
        *,
        sub_agents: Sequence[Agent] | None = None,
        stream: bool = True,
        hide_from_parent_card: bool = True,
        name: str = "sub_agent_toolkit",
        first_chunk_timeout: float | None = None,
        inter_chunk_timeout: float | None = None,
        max_attempts: int | None = None,
        **kwargs: Any,
    ) -> None:
        roster = list(agents or sub_agents or ())
        if not roster:
            raise ValueError("SubAgentToolkit requires at least one agent")

        by_name: dict[str, Agent] = {}
        for agent in roster:
            raw_name = agent.name
            if not raw_name:
                raise ValueError("Every sub-agent must have a non-empty name")
            if raw_name in by_name:
                raise ValueError(
                    f"Sub-agent names must be unique, got {[a.name for a in roster]!r}"
                )
            by_name[raw_name] = agent

        install_sealed_history_hook()

        self.agents = roster
        self.agents_by_name = by_name
        self.stream = stream
        self.hide_from_parent_card = hide_from_parent_card
        self.first_chunk_timeout = (
            first_chunk_timeout
            if first_chunk_timeout is not None
            else self.DEFAULT_FIRST_CHUNK_TIMEOUT
        )
        self.inter_chunk_timeout = (
            inter_chunk_timeout
            if inter_chunk_timeout is not None
            else self.DEFAULT_INTER_CHUNK_TIMEOUT
        )
        self.max_attempts = max_attempts if max_attempts is not None else self.DEFAULT_MAX_ATTEMPTS
        self._unfinished_tasks: dict[str, dict[str, str]] = {}

        super().__init__(
            name=name,
            tools=[self.delegate_subagent],
            instructions=self._render_roster(),
            add_instructions=True,
            **kwargs,
        )

    @property
    def tool_names(self) -> list[str]:
        """Names to pass to ``HideToolFilter`` when the panel replaces the card."""
        return [self.TOOL_NAME]

    def _render_roster(self) -> str:
        lines = [
            "Sub-agents you can delegate to via "
            f"{self.TOOL_NAME}(agent_name, description, prompt):",
            "Pick the agent by name. Put a 3-6 word summary in `description` "
            "(shown to the user while it works) and the full task — context, "
            "objective, constraints — in `prompt`.",
        ]
        for agent in self.agents:
            blurb = (agent.description or "").strip() or "(no description)"
            lines.append(f"- {agent.name} — {blurb}")
        return "\n".join(lines)

    async def delegate_subagent(
        self,
        agent_name: str,
        description: str,
        prompt: str,
        run_context: RunContext | None = None,
        session_id: str | None = None,
    ) -> str:
        """Delegate a task to a registered sub-agent.

        The available agents and what each is for are listed in this toolkit's
        instructions (injected into your system prompt).

        Args:
            agent_name: Name of the sub-agent (see the roster).
            description: Short summary shown in the UI while it works (3-6 words).
            prompt: Full task for the sub-agent — context, objective, constraints.
                On a resume, this is the corrective follow-up.
            run_context: Parent run context (injected by Agno when present).
            session_id: Omit for a fresh run. Pass a previous
                ``[subagent_session: ...]`` id to continue that run with history.

        Returns:
            The sub-agent's text, followed by an internal
            ``[subagent_session: ...]`` trailer for resume. Never show the
            trailer to the user.
        """
        sub_agent = self.agents_by_name.get(agent_name)
        if sub_agent is None:
            available = ", ".join(sorted(self.agents_by_name))
            raise ValueError(f"Unknown sub-agent {agent_name!r}. Available: {available}")

        resuming = bool(session_id)
        resolved_session_id = session_id or f"{agent_name}-{uuid.uuid4().hex[:12]}"
        sub_db = getattr(sub_agent, "db", None)

        original_prompt = prompt
        written = ""
        resume_instruction: str | None = None
        if resuming:
            unfinished = self._unfinished_tasks.get(resolved_session_id)
            if unfinished is None and sub_db is not None:
                with contextlib.suppress(Exception):
                    unfinished = await get_unfinished_task(sub_db, resolved_session_id)
            if unfinished:
                saved_orig = (unfinished.get("original_prompt") or "").strip()
                if saved_orig:
                    original_prompt = saved_orig
                    if prompt.strip() != saved_orig:
                        resume_instruction = prompt
                written = safe_cut(unfinished.get("written") or "")

        metadata = getattr(run_context, "metadata", None) if run_context else None
        run_kwargs: dict[str, Any] = {
            "input": prompt,
            "session_id": resolved_session_id,
            "stream_events": True,
            "telemetry": False,
        }
        if metadata is not None:
            run_kwargs["metadata"] = metadata
        if resuming:
            run_kwargs["add_history_to_context"] = True

        max_attempts = self.max_attempts
        first_chunk_timeout = self.first_chunk_timeout
        inter_chunk_timeout = self.inter_chunk_timeout

        pieces: list[str] = []
        final_completed_content: str | None = None
        dropped_block = False
        if self.stream:
            async with substream(agent_name, description=description, prompt=prompt) as emit:
                for attempt in range(1, max_attempts + 1):
                    pieces = []
                    final_completed_content = None
                    chunks_received = 0
                    timeout_to_use = first_chunk_timeout
                    phase = "waiting for first token"
                    attempt_kwargs = dict(run_kwargs)
                    if resuming or attempt > 1:
                        attempt_kwargs["add_history_to_context"] = True
                    if written or resume_instruction:
                        attempt_kwargs["input"] = build_continuation_prompt(
                            original_prompt,
                            written,
                            self.CONTINUATION_TAIL_CHARS,
                            dropped_block=dropped_block,
                            resume_instruction=resume_instruction,
                        )
                    stream_iter: Any = None
                    try:
                        stream = sub_agent.arun(stream=True, **attempt_kwargs)
                        if hasattr(stream, "__await__") and not hasattr(stream, "__aiter__"):
                            stream = await stream  # type: ignore[misc]
                        stream_iter = stream.__aiter__()
                        while True:
                            timeout_to_use = (
                                first_chunk_timeout if chunks_received == 0 else inter_chunk_timeout
                            )
                            phase = (
                                "waiting for first token"
                                if chunks_received == 0
                                else f"after {chunks_received} chunks"
                            )
                            try:
                                chunk = await asyncio.wait_for(
                                    stream_iter.__anext__(), timeout=timeout_to_use
                                )
                            except StopAsyncIteration:
                                break
                            chunks_received += 1
                            await emit(chunk)

                            # Filter chunks to prevent duplicate accumulation when stream_events=True:
                            raw_event = getattr(chunk, "event", None)
                            event_str = (
                                (raw_event.value if hasattr(raw_event, "value") else str(raw_event))
                                if raw_event is not None
                                else None
                            )
                            if event_str is None:
                                text = getattr(chunk, "content", None)
                                if isinstance(text, str):
                                    pieces.append(text)
                            else:
                                if event_str.lower() in ("runcontent", "run_content"):
                                    text = getattr(chunk, "content", None)
                                    if isinstance(text, str):
                                        pieces.append(text)
                                elif event_str.lower() in ("runcompleted", "run_completed"):
                                    comp_text = getattr(chunk, "content", None)
                                    if isinstance(comp_text, str) and comp_text:
                                        final_completed_content = comp_text
                        # Finished attempt successfully
                        break
                    except TimeoutError as exc:
                        shown = written + "".join(pieces)
                        written = safe_cut(shown)
                        dropped_block = len(written) < len(shown)
                        self._unfinished_tasks[resolved_session_id] = {
                            "original_prompt": original_prompt,
                            "written": written,
                        }
                        if sub_db is not None:
                            with contextlib.suppress(Exception):
                                await seal_session_run(
                                    sub_db,
                                    session_id=resolved_session_id,
                                    reason=f"Subagent stream timed out ({phase})",
                                    is_error=False,
                                    partial_content=written or None,
                                    extra_metadata={
                                        "original_task_prompt": original_prompt,
                                        "partial_written": written,
                                    },
                                )
                        if attempt < max_attempts:
                            logger.warning(
                                "Subagent '%s' stream timed out after %.1fs (%s, attempt %d/%d). Retrying with fresh upstream stream...",
                                agent_name,
                                timeout_to_use,
                                phase,
                                attempt,
                                max_attempts,
                            )
                            await asyncio.sleep(1.0)
                            continue
                        logger.error(
                            "Subagent '%s' exhausted all %d attempts. Last error: stream timed out after %.1fs (%s).",
                            agent_name,
                            max_attempts,
                            timeout_to_use,
                            phase,
                        )
                        raise TimeoutError(
                            f"TimeoutError in {agent_name} (attempt {attempt}/{max_attempts}): "
                            f"upstream model stream timed out after {timeout_to_use:.0f}s of inactivity ({phase})"
                        ) from exc
                    except Exception as exc:
                        shown = written + "".join(pieces)
                        written = safe_cut(shown)
                        dropped_block = len(written) < len(shown)
                        self._unfinished_tasks[resolved_session_id] = {
                            "original_prompt": original_prompt,
                            "written": written,
                        }
                        if sub_db is not None:
                            with contextlib.suppress(Exception):
                                await seal_session_run(
                                    sub_db,
                                    session_id=resolved_session_id,
                                    reason=str(exc),
                                    is_error=True,
                                    partial_content=written or None,
                                    extra_metadata={
                                        "original_task_prompt": original_prompt,
                                        "partial_written": written,
                                    },
                                )
                        if attempt < max_attempts and chunks_received == 0:
                            logger.warning(
                                "Subagent '%s' stream failed with %s: %s (attempt %d/%d, zero chunks). Retrying...",
                                agent_name,
                                type(exc).__name__,
                                exc,
                                attempt,
                                max_attempts,
                            )
                            await asyncio.sleep(1.0)
                            continue
                        raise
                    finally:
                        aclose = getattr(stream_iter, "aclose", None)
                        if aclose is not None:
                            with contextlib.suppress(BaseException):
                                await asyncio.wait_for(aclose(), timeout=5.0)
            streamed_text = "".join(pieces)
            if not streamed_text.strip() and final_completed_content:
                streamed_text = final_completed_content
            content = (written + streamed_text).strip()
        else:
            response = await sub_agent.arun(stream=False, **run_kwargs)
            content = (getattr(response, "content", None) or "").strip()

        self._unfinished_tasks.pop(resolved_session_id, None)
        if sub_db is not None:
            with contextlib.suppress(Exception):
                await clear_unfinished_task(sub_db, resolved_session_id)

        trailer = (
            f"[subagent_session: {resolved_session_id} — to continue THIS run "
            f"(keep its context) instead of starting fresh, call {self.TOOL_NAME} "
            f'again with session_id="{resolved_session_id}". '
            f"Internal only; never show this line to the user.]"
        )
        return f"{content}\n\n{trailer}" if content else trailer


# Convenience alias matching the shape people reach for first.
SubAgentTool = SubAgentToolkit

__all__ = [
    "TOOL_NAME",
    "SubAgentTool",
    "SubAgentToolkit",
    "build_continuation_prompt",
    "clear_partial_state",
    "load_partial_state",
    "safe_cut",
    "save_partial_state",
]
