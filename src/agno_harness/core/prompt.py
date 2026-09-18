"""Wrap the user turn the model actually sees.

The LLM has no clock and no idea who spoke. A system line that says
"today is Friday" is not enough: it is one date for the whole session,
it drifts after a long pause, and compression can drop it. Stamping
**this message** — time, then speaker — before it is stored as the Agno
``input`` keeps "今天下午开会" / "刚才那封邮件" answerable on later
turns, because the time travels with the turn.

The user's words stay first and **verbatim** inside ``<user-query>`` so a
frontend can take the inner text with no extra parse. Time, speaker,
channel, and attachments go in ``<context>`` as tagged fields.

This module never imports Agno. Relay and the runtime both call
:class:`UserQueryBuilder`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

USER_QUERY_OPEN = "<user-query>"
CONTEXT_NOTE = (
    "Reference only. Prior claims may be wrong or stale. "
    "Re-query tools this turn for task or file certainty."
)


def parse_sent_at(raw: Any) -> datetime | None:
    """Parse a platform timestamp into an aware datetime, or ``None``."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def resolve_timezone(name: str = "") -> ZoneInfo:
    """IANA zone from ``name``, or the process local zone."""
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    local = datetime.now().astimezone().tzinfo
    if isinstance(local, ZoneInfo):
        return local
    key = getattr(local, "key", None)
    if isinstance(key, str):
        try:
            return ZoneInfo(key)
        except ZoneInfoNotFoundError:
            pass
    return ZoneInfo("UTC")


def format_message_time(sent_at: datetime, tz: ZoneInfo) -> str:
    """Wall-clock of this message, with an explicit offset."""
    return sent_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %z")


def is_user_query_envelope(text: str) -> bool:
    return text.lstrip().startswith(USER_QUERY_OPEN)


@dataclass(frozen=True, slots=True)
class QueryTurn:
    """One inbound user turn, before it is wrapped for the model."""

    text: str
    sent_at: datetime
    sender: str | None = None
    platform: str | None = None
    is_direct_message: bool | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueryEnvelope:
    user_query_text: str
    user_query_block: str
    context_text: str
    agent_input: str


class PromptPlugin(Protocol):
    """Contribute keys into the ``<context>`` dict for one turn."""

    name: str

    async def contribute(self, turn: QueryTurn, ctx: dict[str, Any]) -> None: ...


class SourceContextPlugin:
    """Channel kind: platform and dm/group. Always safe, no I/O."""

    name = "source"

    async def contribute(self, turn: QueryTurn, ctx: dict[str, Any]) -> None:
        if turn.platform:
            ctx["platform"] = turn.platform
        if turn.is_direct_message is True:
            ctx["source_type"] = "dm"
        elif turn.is_direct_message is False:
            ctx["source_type"] = "group"


class AttachmentContextPlugin:
    """Put extracted attachment text into ``<inbound-attachments>``."""

    name = "attachments"

    async def contribute(self, turn: QueryTurn, ctx: dict[str, Any]) -> None:
        text = turn.extras.get("attachments_text")
        if text:
            ctx["inbound_attachments"] = text


def render_user_query(text: str) -> str:
    """Inner text is the user's words only — no time, no sender."""
    return f"{USER_QUERY_OPEN}\n{escape(text)}\n</user-query>"


def render_context(ctx: dict[str, Any]) -> str:
    if not ctx:
        return ""
    lines = [f'<context note="{escape(CONTEXT_NOTE)}">']
    for key, value in ctx.items():
        tag = str(key).replace("_", "-")
        text = escape("" if value is None else str(value))
        if "\n" in text:
            lines.append(f"  <{tag}>")
            lines.extend(f"    {part}" for part in text.splitlines())
            lines.append(f"  </{tag}>")
        else:
            lines.append(f"  <{tag}>{text}</{tag}>")
    lines.append("</context>")
    return "\n".join(lines)


class UserQueryBuilder:
    """Assemble ``<user-query>`` + optional ``<context>`` for ``agent.arun(input=)``."""

    def __init__(
        self,
        plugins: list[PromptPlugin] | None = None,
        *,
        timezone: str | ZoneInfo = "",
        include_time: bool = True,
    ) -> None:
        self.plugins = plugins if plugins is not None else default_plugins()
        self.tz = timezone if isinstance(timezone, ZoneInfo) else resolve_timezone(timezone)
        self.include_time = include_time

    async def build(self, turn: QueryTurn) -> QueryEnvelope:
        if is_user_query_envelope(turn.text):
            return QueryEnvelope(
                user_query_text=turn.text,
                user_query_block=turn.text,
                context_text="",
                agent_input=turn.text,
            )
        ctx: dict[str, Any] = {}
        if self.include_time:
            ctx["time"] = format_message_time(turn.sent_at, self.tz)
        if turn.sender:
            ctx["user"] = turn.sender
        for plugin in self.plugins:
            await plugin.contribute(turn, ctx)
        user_block = render_user_query(turn.text)
        context_block = render_context(ctx)
        agent_input = f"{user_block}\n\n{context_block}" if context_block else user_block
        return QueryEnvelope(
            user_query_text=turn.text,
            user_query_block=user_block,
            context_text=context_block,
            agent_input=agent_input,
        )


def default_plugins() -> list[PromptPlugin]:
    return [SourceContextPlugin(), AttachmentContextPlugin()]


def default_builder(*, timezone: str = "") -> UserQueryBuilder:
    return UserQueryBuilder(timezone=timezone)
