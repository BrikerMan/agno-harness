"""Chime-in (autonomous interjection) policy engine with least privilege guardrails.

In multi-user group chats (Teams channels, Lark groups), bots receive all conversation
traffic if permissions allow. Without a chime-in policy, an agent either suffers from
expensive token storms (replying to every irrelevant chatter) or stays completely dumb.

This module provides a first-class architectural slot for autonomous interjection,
enforcing least-privilege defaults (e.g. Mention-Only) while allowing downstream
applications to plug in keyword filters, LLM intent triagers, or topic classifiers.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .channel import ChannelEvent

log = logging.getLogger("agno_harness.chimein")


@runtime_checkable
class ChimeInPolicy(Protocol):
    """Protocol for deciding whether an agent should chime in on an incoming event."""

    async def should_chime_in(self, event: ChannelEvent) -> bool:
        """Return True if the agent should autonomously respond; False to remain silent."""
        ...


class MentionOnlyPolicy:
    """Least-privilege policy (default for group chats).

    Always responds to direct 1-on-1 messages.
    In group chats or channel threads, only responds if:
    1. The bot is explicitly mentioned (<at> tag or mention entity); OR
    2. The message is a direct reply to one of the bot's prior messages.
    """

    def __init__(self, bot_names: Sequence[str] | None = None) -> None:
        self.bot_names = [n.lower() for n in (bot_names or [])]

    async def should_chime_in(self, event: ChannelEvent) -> bool:
        # 1. 1-on-1 direct message: always respond
        if event.key.is_direct_message:
            return True

        # 2. Check for mention tag in cleaned Markdown text
        text = event.text or ""
        raw_text = event.raw_text or ""

        # If any <at> tag exists and bot_names is not restricted, treat as mention
        if "<at>" in text or "<at>" in raw_text or "@" in text:
            if not self.bot_names:
                return True
            # If specific bot names given, check if any is inside the mention
            for name in self.bot_names:
                if f"<at>{name}</at>" in text.lower() or f"@{name}" in text.lower():
                    return True

        # 3. Check raw entities for Teams
        if isinstance(event.raw, dict):
            entities = event.raw.get("entities", [])
            for e in entities:
                if isinstance(e, dict) and e.get("type") == "mention":
                    if not self.bot_names:
                        return True
                    mentioned_name = ((e.get("mentioned") or {}).get("name") or "").lower()
                    if any(name in mentioned_name for name in self.bot_names):
                        return True

        # Default in group chats: remain silent (least privilege)
        return False


class AlwaysChimeInPolicy:
    """Greedy policy: chimes in on every inbound message."""

    async def should_chime_in(self, event: ChannelEvent) -> bool:
        return True


class KeywordChimeInPolicy:
    """Chime in if message is direct OR contains any configured trigger keywords.

    Useful for domain assistant bots (e.g. listening for "deploy", "incident", "help").
    """

    def __init__(self, keywords: Sequence[str], allow_direct: bool = True) -> None:
        self.keywords = [k.lower() for k in keywords]
        self.allow_direct = allow_direct

    async def should_chime_in(self, event: ChannelEvent) -> bool:
        if self.allow_direct and event.key.is_direct_message:
            return True

        search_space = f"{event.text} {event.raw_text}".lower()
        return any(kw in search_space for kw in self.keywords)
