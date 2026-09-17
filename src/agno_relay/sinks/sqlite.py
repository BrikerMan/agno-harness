import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from ..core.channel import ChannelEvent, OutboundMessage
from ..sessions.models import ConversationKey
from .base import BaseSink


class SQLiteSink(BaseSink):
    """Asynchronous SQLite sink for persisting inbound and outbound conversation messages."""

    def __init__(self, db_path: str = "messages.db") -> None:
        self.db_path = db_path
        self._initialized = False

    async def _ensure_table(self) -> None:
        if self._initialized:
            return
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT,
                    sender_id TEXT,
                    session_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    cards_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def record_inbound(self, event: ChannelEvent, session_id: str) -> None:
        await self._ensure_table()
        now_iso = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO audit_messages (
                    direction, platform, chat_id, thread_id, sender_id, session_id, text, cards_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "inbound",
                    event.key.platform,
                    event.key.chat_id,
                    event.key.thread_id,
                    event.key.sender_id,
                    session_id,
                    event.text,
                    None,
                    now_iso,
                ),
            )
            await db.commit()

    async def record_outbound(
        self, destination: ConversationKey, message: OutboundMessage, session_id: str
    ) -> None:
        await self._ensure_table()
        now_iso = datetime.now(UTC).isoformat()
        cards_str = json.dumps(message.cards) if message.cards else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO audit_messages (
                    direction, platform, chat_id, thread_id, sender_id, session_id, text, cards_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "outbound",
                    destination.platform,
                    destination.chat_id,
                    destination.thread_id,
                    destination.sender_id,
                    session_id,
                    message.text,
                    cards_str,
                    now_iso,
                ),
            )
            await db.commit()

    async def get_history(self, chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve historical messages for a chat."""
        await self._ensure_table()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM audit_messages
                WHERE chat_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (chat_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
