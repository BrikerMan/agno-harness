"""Human-in-the-loop — pausing a run and resuming it with the user's answer.

AG-UI expresses HITL without any dedicated event type. A run that needs the user
finishes normally, having emitted ``TOOL_CALL_START`` / ``ARGS`` / ``END`` for
each tool that is waiting. The client renders those as a prompt, collects an
answer, and sends the whole conversation back with trailing ``ToolMessage``
entries carrying the results. The server recognises those and continues the
paused run instead of starting a new one.

Agno implements every piece of that (``RunPaused`` is a completion event, and
``on_run_completed`` already renders the waiting tools). What is missing is the
routing decision, which is what this module owns.

Four flavours, all resumed through the same path:

``confirmation``
    ``@tool(requires_confirmation=True)``. The client answers
    ``{"accepted": true}`` or ``{"accepted": false, "note": "..."}``.
``user_input``
    The agent needs field values. The client answers ``{"values": {...}}``.
``user_feedback``
    The agent offers choices. The client answers ``{"selections": {...}}``.
``external_execution``
    The tool runs on the client — browser geolocation, a device command, a
    confirmation only the user's environment can give. Declare these as
    ``RunAgentInput.tools`` and Agno turns them into external-execution
    functions automatically. The client answers with the raw result string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ag_ui.core import EventType, RunAgentInput, ToolCallResultEvent
from ag_ui.core.types import ToolMessage as AGUIToolMessage
from agno.os.interfaces.agui.input import extract_tool_messages, parse_client_tools
from agno.os.interfaces.agui.resume import (
    ensure_requirements_resolved,
    resolve_requirements_from_tool_messages,
    resume_paused_run,
)

PAUSE_TYPES = ("confirmation", "user_input", "user_feedback", "external_execution")


@dataclass(frozen=True)
class ResumeRequest:
    """A resume, recognised from the trailing tool messages of the input."""

    tool_messages: list[AGUIToolMessage]

    @property
    def tool_call_ids(self) -> list[str]:
        return [m.tool_call_id for m in self.tool_messages]


def detect_resume(run_input: RunAgentInput) -> ResumeRequest | None:
    """Return a :class:`ResumeRequest` if the input answers a paused run.

    The signal is ``ToolMessage`` entries at the *end* of the message list:
    anything earlier is ordinary history from a previous turn.
    """
    tool_messages = extract_tool_messages(run_input.messages or [])
    if not tool_messages:
        return None
    return ResumeRequest(tool_messages=list(tool_messages))


def extract_resume_input(resume: ResumeRequest) -> str:
    """Extract triggering answer text from trailing tool messages on resume."""
    parts: list[str] = []
    for msg in resume.tool_messages:
        content = getattr(msg, "content", None)
        if not content:
            continue
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, (dict, list)):
            parts.append(json.dumps(content, ensure_ascii=False))
        else:
            parts.append(str(content))
    return "\n".join(parts)


def client_tool_functions(run_input: RunAgentInput) -> list[Any] | None:
    """Convert client-declared tools into external-execution Agno functions."""
    return parse_client_tools(run_input.tools) or None


def describe_pause(chunk: Any) -> list[dict[str, Any]]:
    """Summarize what a ``RunPaused`` chunk is waiting for.

    Agno already emits the tool-call events a client needs to render the prompt;
    this is the machine-readable companion, handy for a ``CUSTOM`` event or the
    debug UI so the client knows *which kind* of answer each tool wants rather
    than inferring it from the tool name.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in _pause_tools(chunk):
        tool_call_id = getattr(tool, "tool_call_id", None)
        pause_type = _pause_type(tool)
        if not tool_call_id or pause_type is None or tool_call_id in seen:
            continue
        seen.add(tool_call_id)
        out.append(
            {
                "pauseType": pause_type,
                "toolCallId": tool_call_id,
                "toolName": getattr(tool, "tool_name", None),
                "toolArgs": getattr(tool, "tool_args", None) or {},
                "userInputSchema": _schema_for(tool, pause_type),
            }
        )
    return out


def resume_result_events(resume: ResumeRequest) -> list[ToolCallResultEvent]:
    """Turn trailing ToolMessages into ``TOOL_CALL_RESULT`` frames for replay."""
    return [
        ToolCallResultEvent(
            type=EventType.TOOL_CALL_RESULT,
            message_id=f"tool-result-{message.tool_call_id}",
            tool_call_id=message.tool_call_id,
            content=message.content or "",
            role="tool",
        )
        for message in resume.tool_messages
    ]


def _pause_tools(chunk: Any) -> list[Any]:
    collected: list[Any] = []
    for attr in (
        "tools",
        "tools_requiring_confirmation",
        "tools_requiring_user_input",
        "tools_awaiting_external_execution",
    ):
        collected.extend(getattr(chunk, attr, None) or [])
    return collected


def _pause_type(tool: Any) -> str | None:
    """Same priority as Agno ``RunRequirement.pause_type``: feedback > external > input > confirmation."""
    if getattr(tool, "user_feedback_schema", None):
        return "user_feedback"
    if getattr(tool, "external_execution_required", None):
        return "external_execution"
    if getattr(tool, "requires_user_input", None) or getattr(tool, "user_input_schema", None):
        return "user_input"
    if getattr(tool, "requires_confirmation", None):
        return "confirmation"
    return None


def _schema_for(tool: Any, pause_type: str) -> list[dict[str, Any]] | None:
    if pause_type == "user_input":
        return _user_input_schema(tool)
    if pause_type == "user_feedback":
        return _user_feedback_schema(tool)
    return None


def _user_input_schema(tool: Any) -> list[dict[str, Any]] | None:
    """Field descriptors for a ``user_input`` pause, if the tool declared any."""
    fields = getattr(tool, "user_input_schema", None)
    if not fields:
        return None
    described: list[dict[str, Any]] = []
    for field in fields:
        described.append(
            {
                "name": getattr(field, "name", None),
                "description": getattr(field, "description", None),
                "fieldType": _type_name(getattr(field, "field_type", None)),
                "value": getattr(field, "value", None),
            }
        )
    return described


def _user_feedback_schema(tool: Any) -> list[dict[str, Any]] | None:
    questions = getattr(tool, "user_feedback_schema", None)
    if not questions:
        return None
    described: list[dict[str, Any]] = []
    for question in questions:
        described.append(
            {
                # Resume keys ``selections`` by question text, not header.
                "name": getattr(question, "question", None),
                "description": getattr(question, "header", None)
                or getattr(question, "question", None),
                "fieldType": "choice",
                "value": getattr(question, "selected_options", None),
                "options": [
                    getattr(option, "label", None) or str(option)
                    for option in (getattr(question, "options", None) or [])
                ],
                "multiSelect": bool(getattr(question, "multi_select", False)),
            }
        )
    return described


def _type_name(field_type: Any) -> str | None:
    if field_type is None:
        return None
    return getattr(field_type, "__name__", None) or str(field_type)


def encode_tool_result(value: Any) -> str:
    """Serialize a client-side tool result into the string a ``ToolMessage`` holds."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


__all__ = [
    "PAUSE_TYPES",
    "ResumeRequest",
    "client_tool_functions",
    "describe_pause",
    "detect_resume",
    "encode_tool_result",
    "ensure_requirements_resolved",
    "extract_resume_input",
    "resolve_requirements_from_tool_messages",
    "resume_paused_run",
    "resume_result_events",
]
