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
import re
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
    1. The bot is explicitly mentioned (<at> tag, mention entity, or @name); OR
    2. The message is a direct reply to one of the bot's prior messages.
    """

    def __init__(
        self,
        bot_names: Sequence[str] | None = None,
        bot_ids: Sequence[str] | None = None,
    ) -> None:
        self.bot_names = [n.lower() for n in (bot_names or [])]
        self.bot_ids = [str(i).lower() for i in (bot_ids or [])]

    async def should_chime_in(self, event: ChannelEvent) -> bool:
        # 1. 1-on-1 direct message: always respond
        if event.key.is_direct_message:
            return True

        # Extract bot candidate IDs and names
        target_ids = {b.removeprefix("28:").strip() for b in self.bot_ids if b and b.strip()}
        target_names = {n.strip() for n in self.bot_names if n and n.strip()}

        # In Teams Bot Framework, event.raw['recipient'] is the receiving bot
        if isinstance(event.raw, dict):
            recipient = event.raw.get("recipient")
            if isinstance(recipient, dict):
                r_id = str(recipient.get("id") or "").lower().removeprefix("28:").strip()
                if r_id:
                    target_ids.add(r_id)
                r_name = str(recipient.get("name") or "").lower().strip()
                if r_name:
                    target_names.add(r_name)
                r_aad = str(recipient.get("aadObjectId") or "").lower().strip()
                if r_aad:
                    target_ids.add(r_aad)

        # 2. Check raw entities for Teams / platform mentions
        has_mention_entities = False
        if isinstance(event.raw, dict):
            entities = event.raw.get("entities", [])
            for e in entities:
                if isinstance(e, dict) and e.get("type") == "mention":
                    has_mention_entities = True
                    mentioned = e.get("mentioned") or {}
                    if isinstance(mentioned, dict):
                        m_id = str(mentioned.get("id") or "").lower().removeprefix("28:").strip()
                        m_aad = str(mentioned.get("aadObjectId") or "").lower().strip()
                        m_name = str(mentioned.get("name") or "").lower().strip()

                        if target_ids and (m_id in target_ids or (m_aad and m_aad in target_ids)):
                            return True
                        if target_names and m_name in target_names:
                            return True

        # If the platform delivered explicit mention entities, but none was the bot,
        # the user explicitly addressed other people. Do not chime in.
        if has_mention_entities:
            return False

        # 3. Check for mention tag in cleaned Markdown text or raw_text (excluding quoted blocks)
        stripped_raw = re.sub(
            r"<blockquote[\s\S]*?</blockquote>", "", event.raw_text or "", flags=re.IGNORECASE
        )
        stripped_text = "\n".join(
            line for line in (event.text or "").splitlines() if not line.strip().startswith(">")
        )
        search_space = f"{stripped_text} {stripped_raw}".lower()

        for name in target_names:
            if not name:
                continue
            if (
                f"<at>{name}</at>" in search_space
                or f"@{name}" in search_space
                or re.search(rf"<at[^>]*>\s*{re.escape(name)}\s*</at>", search_space)
            ):
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
