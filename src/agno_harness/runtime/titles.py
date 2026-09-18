"""Thread titles — a short LLM name, persisted on the Agno session.

The sidebar wants a display name, not a UUID and not the first forty characters
of the user's message. This module does one short completion against
``agent.model`` (never ``agent.arun()``, which would pollute the session and
fire tools) and writes the result to ``session_data["session_name"]``.

There is no HTTP route. The product either calls
:meth:`~agno_harness.runtime.runtime.AgentRuntime.generate_thread_title`
or hangs :func:`make_thread_title_hook` as a post-run hook. The hook yields a
``CUSTOM`` ``thread.title`` event so the same SSE the client is already reading
can crossfade the sidebar row. A later round re-titles by sending
``forwardedProps.refreshTitle = true`` on the next run.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import BaseEvent, CustomEvent, EventType

from .replay import input_to_text
from .scope import RunScope
from .types import PostRunHook, PreRunHook

EVENT_THREAD_TITLE = "thread.title"

_TITLE_PROMPT = (
    "Write a short sidebar title for this chat. "
    "Use the same language as the user; do not translate. "
    "4-8 words, no quotes, no trailing punctuation, no explanation.\n\n"
)


def saved_session_title(session: Any) -> str | None:
    """The display name stored on the session, or ``None`` if it was never named."""
    data = getattr(session, "session_data", None) or {}
    if not isinstance(data, dict):
        return None
    name = data.get("session_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def completion_text(completion: Any) -> str:
    """Plain text off Agno's terminal chunk, if it has any."""
    if completion is None:
        return ""
    content = getattr(completion, "content", None)
    return content if isinstance(content, str) else ""


def _forwarded_flag(props: Any, name: str) -> bool:
    if props is None:
        return False
    if isinstance(props, dict):
        return bool(props.get(name))
    getter = getattr(props, "get", None)
    if callable(getter):
        return bool(getter(name))
    return bool(getattr(props, name, False))


def _transcript(*, user_text: str, assistant_text: str) -> str:
    lines: list[str] = []
    if user_text:
        lines.append(f"User: {user_text}")
    if assistant_text:
        lines.append(f"Assistant: {assistant_text}")
    return "\n".join(lines)


def _transcript_from_session(session: Any, *, max_turns: int) -> str:
    runs = list(getattr(session, "runs", None) or [])
    if max_turns > 0:
        runs = runs[-max_turns:]
    lines: list[str] = []
    for run in runs:
        user = input_to_text(getattr(run, "input", None))
        assistant = getattr(run, "content", None) or ""
        if not isinstance(assistant, str):
            assistant = str(assistant) if assistant else ""
        if user:
            lines.append(f"User: {user}")
        if assistant:
            lines.append(f"Assistant: {assistant}")
    return "\n".join(lines)


def _clean_title(raw: str) -> str:
    line = raw.strip().splitlines()[0].strip().strip("\"'`") if raw.strip() else ""
    return line[:80]


async def _ask_model(agent: Any, transcript: str) -> str | None:
    model = getattr(agent, "model", None)
    aresponse = getattr(model, "aresponse", None)
    if not transcript.strip() or not callable(aresponse):
        return None
    try:
        from agno.models.message import Message
    except ImportError:  # pragma: no cover - Agno is a hard dependency of runtime
        return None
    response = await aresponse([Message(role="user", content=f"{_TITLE_PROMPT}{transcript}")])
    content = getattr(response, "content", None)
    if not isinstance(content, str):
        return None
    title = _clean_title(content)
    return title or None


async def _persist_title(db: Any, thread_id: str, title: str, *, user_id: str | None) -> None:
    if db is None:
        return
    rename = getattr(db, "rename_session", None)
    if callable(rename):
        res = rename(thread_id, None, title, user_id=user_id)
        if inspect.isawaitable(res):
            await res
        return
    get_session = getattr(db, "get_session", None)
    if not callable(get_session):
        return
    session = get_session(session_id=thread_id, user_id=user_id)
    if inspect.isawaitable(session):
        session = await session
    if session is None:
        return
    data = getattr(session, "session_data", None)
    if data is None:
        session.session_data = {}
        data = session.session_data
    if isinstance(data, dict):
        data["session_name"] = title
    upsert = getattr(db, "upsert_session", None)
    if callable(upsert):
        res = upsert(session)
        if inspect.isawaitable(res):
            await res


async def generate_thread_title(
    *,
    agent: Any,
    db: Any,
    thread_id: str,
    user_id: str | None = None,
    max_turns: int = 1,
    user_text: str | None = None,
    completion_text: str | None = None,
) -> str | None:
    """Read recent turns, ask the model for a sidebar title, persist it.

    Pass ``user_text`` / ``completion_text`` when the Agno session may not have
    flushed this run yet (the usual post-run case). Otherwise the last
    ``max_turns`` runs on the session are used.
    """
    if user_text or completion_text:
        transcript = _transcript(user_text=user_text or "", assistant_text=completion_text or "")
    else:
        get_session = getattr(db, "get_session", None) if db is not None else None
        if callable(get_session):
            session = get_session(session_id=thread_id, user_id=user_id)
            if inspect.isawaitable(session):
                session = await session
        else:
            session = None
        transcript = _transcript_from_session(session, max_turns=max_turns)
    try:
        title = await _ask_model(agent, transcript)
    except Exception:  # noqa: BLE001 - naming must never fail the main run
        return None
    if not title:
        return None
    try:
        await _persist_title(db, thread_id, title, user_id=user_id)
    except Exception:  # noqa: BLE001
        return title
    return title


def make_thread_title_pre_hook(runtime: Any) -> PreRunHook:
    """Pre-run hook: start generating the thread title concurrently on user input.

    Launches a background task off the user's initial prompt so title generation
    runs in parallel with the model's main streaming response, eliminating end-of-run
    hangs on the first turn.
    """

    async def thread_title_pre_hook(scope: RunScope) -> AsyncIterator[BaseEvent]:
        refresh = _forwarded_flag(scope.forwarded_props, "refreshTitle")
        if refresh or not scope.user_text:
            return
        session = None
        get_session = getattr(getattr(runtime, "threads", None), "get_session", None)
        if callable(get_session):
            session = get_session(scope.thread_id, user_id=scope.user_id)
            if inspect.isawaitable(session):
                session = await session
        if saved_session_title(session):
            return

        # Launch concurrent background task on user text
        scope.data["_title_task"] = asyncio.create_task(
            generate_thread_title(
                agent=runtime.agent,
                db=runtime.db,
                thread_id=scope.thread_id,
                user_id=scope.user_id,
                max_turns=1,
                user_text=scope.user_text,
            ),
            name=f"title-{scope.thread_id}",
        )
        if False:
            yield  # type: ignore[misc]

    return thread_title_pre_hook


def make_thread_title_hook(runtime: Any, *, timeout: float = 0.3) -> PostRunHook:
    """Post-run hook: collect or finish naming the thread without blocking the user.

    If a title task was started during pre-run, checks its result (or waits briefly up to
    ``timeout`` seconds so fast/mock models satisfy protocol order). If the task takes
    longer, it continues in the background to persist the title, allowing the main run
    to finish immediately without UI freezing.
    """

    async def thread_title_hook(
        scope: RunScope, *, completion: Any, error: BaseException | None
    ) -> AsyncIterator[BaseEvent]:
        if error is not None:
            task = scope.data.get("_title_task")
            if task is not None and not task.done():
                task.cancel()
            return

        refresh = _forwarded_flag(scope.forwarded_props, "refreshTitle")
        task = scope.data.get("_title_task")
        if task is None:
            session = None
            get_session = getattr(getattr(runtime, "threads", None), "get_session", None)
            if callable(get_session):
                session = get_session(scope.thread_id, user_id=scope.user_id)
                if inspect.isawaitable(session):
                    session = await session
            if not refresh and saved_session_title(session):
                return

            task = asyncio.create_task(
                generate_thread_title(
                    agent=runtime.agent,
                    db=runtime.db,
                    thread_id=scope.thread_id,
                    user_id=scope.user_id,
                    max_turns=2 if refresh else 1,
                    user_text=None if refresh else scope.user_text,
                    completion_text=None if refresh else completion_text(completion),
                ),
                name=f"title-{scope.thread_id}",
            )
            scope.data["_title_task"] = task

        title = None
        if task.done():
            with contextlib.suppress(Exception):
                title = task.result()
        elif timeout > 0:
            try:
                title = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except (TimeoutError, Exception):
                title = None

        if not title:
            return
        yield CustomEvent(
            type=EventType.CUSTOM,
            name=EVENT_THREAD_TITLE,
            value={"threadId": scope.thread_id, "title": title},
        )

    return thread_title_hook


def setup_thread_titles(runtime: Any, *, timeout: float = 0.3) -> None:
    """Register both pre-run and post-run hooks for non-blocking thread naming."""
    runtime.on_pre_run(make_thread_title_pre_hook(runtime))
    runtime.on_post_run(make_thread_title_hook(runtime, timeout=timeout))


__all__ = [
    "EVENT_THREAD_TITLE",
    "completion_text",
    "generate_thread_title",
    "make_thread_title_hook",
    "make_thread_title_pre_hook",
    "saved_session_title",
    "setup_thread_titles",
]
