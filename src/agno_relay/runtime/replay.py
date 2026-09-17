"""History replay — rebuild the conversation from persisted runs.

A refreshed thread must look exactly like it did while streaming. That is harder
than it sounds, because live and history come from completely different sources:
live is a stream of deltas run through parsers and filters, history is a row in
a database. Anything applied on one side has to be applied on the other, or the
page changes under the user when they reload.

So every content-derived transformation lives in one place and both paths call
it. The ``stream-ui`` fence is extracted by the same parser, hidden tools are
hidden by the same name set, injected ``CUSTOM`` events are re-attached to the
run that produced them.

This path exists for runtimes with no event log, which is a legitimate
configuration. With a history archive (or a still-hot Redis log), prefer
``ThreadService.read_frames``: it returns what the client was sent instead of
reconstructing an approximation of it.

Sub-agents are the one thing history cannot read out of the session, because
Agno persists the parent run and knows nothing about a delegation. Frame-based
replay covers them — the boundary events and everything between are in the log —
so this path returns the parent's own content and leaves the panels to it.

What is deliberately *not* restored: ``STEP_STARTED`` / ``STEP_FINISHED``. Those
mark work in flight, and there is no work in flight in a replay.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.streamui import CardCatalog, blocks_to_payload, parse_streamui_text


def input_to_text(input_obj: Any) -> str:
    """Pull the user's text out of a persisted ``RunInput``."""
    if input_obj is None:
        return ""
    content = getattr(input_obj, "input_content", input_obj)
    if isinstance(content, str):
        return content
    message_content = getattr(content, "content", None)
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        return " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in message_content
        )
    return str(content) if content else ""


def last_user_text(messages: Any) -> str:
    """The latest user turn on an AG-UI ``RunAgentInput``.

    Frames start at ``RUN_STARTED``; they never carry the prompt. Agno also
    writes the session only when the run ends. The text has to be copied off
    the request at start so a reload mid-run can still show what was asked.
    """
    for item in reversed(list(messages or [])):
        role = getattr(item, "role", None)
        if role is None and isinstance(item, dict):
            role = item.get("role")
        if str(role) != "user":
            continue
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            text = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
            if text.strip():
                return text
    return ""


def tool_to_dict(tool: Any) -> dict[str, Any]:
    """Convert a persisted ``ToolExecution`` into the client's tool-call shape."""
    args = getattr(tool, "tool_args", None) or {}
    try:
        args_str = json.dumps(args, ensure_ascii=False)
    except TypeError:
        args_str = "{}"
    return {
        "id": getattr(tool, "tool_call_id", None) or "",
        "name": getattr(tool, "tool_name", None) or "tool",
        "args": args_str,
        "status": "error" if getattr(tool, "tool_call_error", False) else "done",
        "result": getattr(tool, "result", None) or "",
        "elapsed": getattr(tool, "metrics", None) and getattr(tool.metrics, "duration", None),
    }


def run_to_messages(
    run: Any,
    *,
    custom_events: list[dict[str, Any]] | None = None,
    hidden_tool_names: set[str] | None = None,
    catalog: CardCatalog | None = None,
) -> list[dict[str, Any]]:
    """Turn one persisted run into ``[user_message, assistant_message]``.

    ``custom_events`` are the events injected during that run (already filtered
    to this ``run_id`` by the caller); they ride along on the assistant message
    so the client can render them exactly where they appeared live.
    """
    hidden = hidden_tool_names or set()
    messages: list[dict[str, Any]] = []
    run_id = getattr(run, "run_id", "") or ""
    injected = list(custom_events or [])

    user_text = input_to_text(getattr(run, "input", None))
    if user_text:
        messages.append({"id": f"u-{run_id}", "role": "user", "content": user_text})

    raw_content = getattr(run, "content", None)
    content = (
        raw_content if isinstance(raw_content, str) else (str(raw_content) if raw_content else "")
    )
    # The same extraction live streaming performs, so the fence never shows up
    # as raw JSON after a reload.
    content, blocks = parse_streamui_text(content, catalog)

    reasoning = getattr(run, "reasoning_content", None) or ""
    tool_calls = [
        tool_to_dict(tool)
        for tool in (getattr(run, "tools", None) or [])
        if (getattr(tool, "tool_name", "") or "") not in hidden
    ]

    # Ensure context compression event from run metadata is present
    run_meta = getattr(run, "metadata", None) or {}
    if isinstance(run_meta, str):
        try:
            run_meta = json.loads(run_meta)
        except Exception:
            run_meta = {}
    checkpoint_meta = run_meta.get("checkpoint") if isinstance(run_meta, dict) else None
    if isinstance(checkpoint_meta, str):
        try:
            checkpoint_meta = json.loads(checkpoint_meta)
        except Exception:
            checkpoint_meta = None

    if checkpoint_meta and not any(
        e.get("name") in ("context.compression", "context_checkpoint") for e in injected
    ):
        injected.insert(
            0,
            {
                "name": "context.compression",
                "value": {
                    "original_tokens": checkpoint_meta.get("original_tokens"),
                    "compacted_tokens": checkpoint_meta.get("compacted_tokens"),
                    "saved_tokens": checkpoint_meta.get("saved_tokens"),
                    "checkpoint": checkpoint_meta.get("content"),
                },
            },
        )

    if content or reasoning or tool_calls or blocks or injected:
        messages.append(
            {
                "id": f"a-{run_id}",
                "role": "assistant",
                "content": content,
                "reasoning": reasoning or None,
                "toolCalls": tool_calls or None,
                "uiBlocks": blocks_to_payload(blocks) or None,
                "customEvents": injected or None,
            }
        )
    return messages


def session_to_messages(
    session: Any,
    *,
    custom_events_by_run: dict[str, list[dict[str, Any]]] | None = None,
    hidden_tool_names: set[str] | None = None,
    catalog: CardCatalog | None = None,
) -> list[dict[str, Any]]:
    """Flatten every run in a session into one oldest-first message list."""
    by_run = custom_events_by_run or {}
    messages: list[dict[str, Any]] = []
    for run in getattr(session, "runs", None) or []:
        run_id = getattr(run, "run_id", "") or ""
        messages.extend(
            run_to_messages(
                run,
                custom_events=by_run.get(run_id),
                hidden_tool_names=hidden_tool_names,
                catalog=catalog,
            )
        )
    return messages


def session_title(session: Any, *, limit: int = 40) -> str:
    """Saved display name, else the first user message, for the thread list."""
    data = getattr(session, "session_data", None) or {}
    if isinstance(data, dict):
        name = data.get("session_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    for run in getattr(session, "runs", None) or []:
        text = input_to_text(getattr(run, "input", None))
        if text:
            return text[:limit]
    return "(empty)"


def raw_session_title(session: Any, *, limit: int = 40) -> str:
    """Title from a session object or Agno raw ``deserialize=False`` dict.

    Avoids hydrating ``RunOutput`` graphs — list endpoints only need a string.
    """
    if isinstance(session, dict):
        data = session.get("session_data") or {}
        if isinstance(data, dict):
            name = data.get("session_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        for run in session.get("runs") or []:
            text = _raw_run_input_text(run)
            if text:
                return text[:limit]
        return "(empty)"
    return session_title(session, limit=limit)


def _raw_run_input_text(run: Any) -> str:
    """Peek the user prompt from a run object or raw dict without full deserialize."""
    if run is None:
        return ""
    if isinstance(run, dict):
        inp = run.get("input")
        if isinstance(inp, dict):
            content = inp.get("input_content", inp.get("content", inp))
            return (
                input_to_text(content)
                if not isinstance(content, (dict, list))
                else (
                    content.get("content", "")
                    if isinstance(content, dict)
                    else input_to_text(content)
                )
            )
        if inp is not None:
            return input_to_text(inp)
        # Some dumps store the prompt at the top level.
        for key in ("message", "content"):
            val = run.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return ""
    return input_to_text(getattr(run, "input", None))


def thread_summary_from_session(session: Any) -> dict[str, Any] | None:
    """Sidebar row for one session. ``None`` when there are no runs.

    ``runCount`` is the number of user turns (Agno runs). ``messageCount`` is
    deprecated and always ``0`` — older clients may still read the field.
    """
    if isinstance(session, dict):
        runs = session.get("runs") or []
        if not runs:
            return None
        return {
            "threadId": session.get("session_id") or "",
            "title": raw_session_title(session),
            "messageCount": 0,
            "runCount": len(runs),
            "updatedAt": session.get("updated_at") or session.get("created_at"),
        }

    runs = getattr(session, "runs", None) or []
    if not runs:
        return None
    return {
        "threadId": getattr(session, "session_id", "") or "",
        "title": session_title(session),
        "messageCount": 0,
        "runCount": len(runs),
        "updatedAt": getattr(session, "updated_at", None) or getattr(session, "created_at", None),
    }


__all__ = [
    "input_to_text",
    "last_user_text",
    "raw_session_title",
    "run_to_messages",
    "session_title",
    "session_to_messages",
    "thread_summary_from_session",
    "tool_to_dict",
]
