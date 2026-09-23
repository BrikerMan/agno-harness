from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from .mixins import load_json, store_json
from .sql_models import get_or_create_action_model


class SQLAlchemyActionStore:
    """Store for tracking pending and completed interactive card actions.

    Records context (session_id, run_id, tool_call_id, meta, payload)
    when an interactive card is sent to Teams/Lark, and retrieves it when
    the user clicks a button.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        model: type[Any] | None = None,
        table_name: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.model = model or get_or_create_action_model(table_name)

    async def create_action(
        self,
        action_id: str,
        session_key: str,
        agno_session_id: str,
        payload: Mapping[str, Any],
        *,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> str:
        """Create a new pending interactive action record. Returns action_ref UUID."""
        action_ref = str(uuid4())
        payload_with_ref = {**dict(payload), "_action_ref": action_ref}
        async with self.session_factory() as session, session.begin():
            row = self.model(
                action_id=action_id,
                session_key=session_key,
                agno_session_id=agno_session_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                status="pending",
                payload_json=store_json(payload_with_ref),
                meta_json=store_json(dict(meta)) if meta else None,
            )
            session.add(row)
        return action_ref

    async def get_action(
        self,
        *,
        action_id: str | None = None,
        tool_call_id: str | None = None,
        session_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Find an action record by action_id, tool_call_id, or session_key."""
        async with self.session_factory() as session:
            stmt = select(self.model)
            if tool_call_id:
                stmt = stmt.where(self.model.tool_call_id == tool_call_id)
            elif action_id:
                stmt = stmt.where(self.model.action_id == action_id)
            if session_key:
                stmt = stmt.where(self.model.session_key == session_key)
            stmt = stmt.order_by(self.model.id.desc())
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if not row:
                return None
            return {
                "id": row.id,
                "action_id": row.action_id,
                "session_key": row.session_key,
                "agno_session_id": row.agno_session_id,
                "run_id": row.run_id,
                "tool_call_id": row.tool_call_id,
                "status": row.status,
                "payload": _object(row.payload_json),
                "meta": _object(row.meta_json),
                "result": _object(row.result_json),
            }

    async def update_status(
        self,
        action_id: str,
        status: str,
        *,
        tool_call_id: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        """Mark an action as approved, rejected, or completed with its result."""
        async with self.session_factory() as session, session.begin():
            stmt = select(self.model)
            if tool_call_id:
                stmt = stmt.where(self.model.tool_call_id == tool_call_id)
            else:
                stmt = stmt.where(self.model.action_id == action_id)
            stmt = stmt.order_by(self.model.id.desc())
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                row.status = status
                if result:
                    row.result_json = store_json(dict(result))


class InMemoryActionStore:
    """In-memory store for tracking interactive card actions and HITL approvals.

    Ideal for local development, unit tests, and lightweight deployments without SQL.
    """

    def __init__(self) -> None:
        self._actions: list[dict[str, Any]] = []

    async def create_action(
        self,
        action_id: str,
        session_key: str,
        agno_session_id: str,
        payload: Mapping[str, Any],
        *,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> str:
        """Create a new pending interactive action record. Returns action_ref UUID."""
        action_ref = str(uuid4())
        payload_with_ref = {**dict(payload), "_action_ref": action_ref}
        record = {
            "id": len(self._actions) + 1,
            "action_id": action_id,
            "session_key": session_key,
            "agno_session_id": agno_session_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "status": "pending",
            "payload": payload_with_ref,
            "meta": dict(meta) if meta else {},
            "result": {},
        }
        self._actions.append(record)
        return action_ref

    async def get_action(
        self,
        *,
        action_id: str | None = None,
        tool_call_id: str | None = None,
        session_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Find an action record by tool_call_id, action_id, or session_key."""
        for record in reversed(self._actions):
            if tool_call_id and record.get("tool_call_id") == tool_call_id:
                return dict(record)
            if action_id and record.get("action_id") == action_id:
                return dict(record)
            if session_key and record.get("session_key") == session_key:
                return dict(record)
        return None

    async def update_status(
        self,
        action_id: str,
        status: str,
        *,
        tool_call_id: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        """Mark an action as approved, rejected, or completed with its result.

        Prefer ``tool_call_id`` when provided (HITL clicks share action_id like
        ``agno.hitl.resume``); otherwise match by ``action_id``. Matches
        SQLAlchemyActionStore filtering so one click cannot approve sibling rows.
        """
        for record in reversed(self._actions):
            if tool_call_id:
                matched = record.get("tool_call_id") == tool_call_id
            else:
                matched = record.get("action_id") == action_id
            if not matched:
                continue
            record["status"] = status
            if result:
                record["result"] = dict(result)


def _object(value: Any) -> dict[str, Any]:
    loaded = load_json(value)
    return loaded if isinstance(loaded, dict) else {}


__all__ = ["InMemoryActionStore", "SQLAlchemyActionStore"]
