import json
import logging
from datetime import UTC, datetime
from typing import Any

from ..core.channel import ChannelEvent, OutboundMessage
from ..sessions.models import ConversationKey
from .base import BaseSink

log = logging.getLogger("agno_harness.sinks.postgres")


class PostgresSink(BaseSink):
    """Asynchronous PostgreSQL sink for persisting inbound and outbound conversation messages."""

    def __init__(self, dsn: str, table_name: str = "audit_messages") -> None:
        self.dsn = dsn
        self.table_name = table_name
        self._pool: Any = None
        self._initialized = False

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg
        except ImportError as exc:
            msg = (
                "PostgresSink requires the 'asyncpg' package. "
                "Install it with `pip install 'agno-harness[postgres]'` or `pip install asyncpg`."
            )
            raise ImportError(msg) from exc

        self._pool = await asyncpg.create_pool(self.dsn)
        return self._pool

    async def _ensure_table(self) -> None:
        if self._initialized:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    id BIGSERIAL PRIMARY KEY,
                    direction VARCHAR(16) NOT NULL,
                    platform VARCHAR(32) NOT NULL,
                    chat_id VARCHAR(128) NOT NULL,
                    thread_id VARCHAR(128),
                    sender_id VARCHAR(128),
                    session_id VARCHAR(128) NOT NULL,
                    text TEXT NOT NULL,
                    raw_text TEXT,
                    raw_payload_json JSONB,
                    cards_json JSONB,
                    extra_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
        self._initialized = True

    async def record_inbound(self, event: ChannelEvent, session_id: str) -> None:
        await self._ensure_table()
        pool = await self._get_pool()
        now_utc = datetime.now(UTC)
        raw_payload_json = json.dumps(event.raw, default=str) if event.raw is not None else None
        extra_dict: dict[str, Any] = {}
        if event.action_id:
            extra_dict["action_id"] = event.action_id
            extra_dict["action_value"] = event.action_value
        if event.attachments:
            extra_dict["attachments"] = [a.model_dump(mode="json") for a in event.attachments]
        extra_json = json.dumps(extra_dict) if extra_dict else None

        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self.table_name} (
                    direction, platform, chat_id, thread_id, sender_id, session_id,
                    text, raw_text, raw_payload_json, cards_json, extra_json, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                "inbound",
                event.key.platform,
                event.key.chat_id,
                event.key.thread_id,
                event.key.sender_id,
                session_id,
                event.text,
                event.raw_text or event.text,
                raw_payload_json,
                None,
                extra_json,
                now_utc,
            )

    async def record_outbound(
        self, destination: ConversationKey, message: OutboundMessage, session_id: str
    ) -> None:
        await self._ensure_table()
        pool = await self._get_pool()
        now_utc = datetime.now(UTC)
        cards_json = json.dumps(message.cards) if message.cards else None
        extra_json = json.dumps(message.extra) if message.extra else None
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self.table_name} (
                    direction, platform, chat_id, thread_id, sender_id, session_id,
                    text, raw_text, raw_payload_json, cards_json, extra_json, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                "outbound",
                destination.platform,
                destination.chat_id,
                destination.thread_id,
                destination.sender_id,
                session_id,
                message.text,
                message.raw_text or message.text,
                None,
                cards_json,
                extra_json,
                now_utc,
            )

    async def close(self) -> None:
        """Close connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
