"""Store protocols and their async SQLAlchemy implementations.

The runtime talks to stores through narrow protocols, never to SQLAlchemy
directly. Anything satisfying the protocol works — an in-memory dict for tests,
a Redis client, your own repository layer — and :class:`SQLAlchemyCustomEventStore`
is the batteries-included option for the common case.

Everything is async and driver-agnostic: SQLite via ``aiosqlite`` and PostgreSQL
via ``asyncpg`` both work through the same code, since nothing here reaches for
dialect-specific SQL.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .mixins import load_json, record_to_dict, store_json


@runtime_checkable
class CustomEventStore(Protocol):
    """Persistence for injected ``CUSTOM`` events."""

    async def save(self, thread_id: str, run_id: str, name: str, value: Any) -> None: ...

    async def list_by_thread(self, thread_id: str) -> list[dict[str, Any]]: ...

    async def delete_by_thread(self, thread_id: str) -> int: ...


class _SQLAlchemyStore:
    """Shared plumbing: a session factory plus your mapped model."""

    def __init__(self, session_factory: async_sessionmaker[Any], model: type[Any]) -> None:
        self._session_factory = session_factory
        self._model = model

    @property
    def model(self) -> type[Any]:
        return self._model

    async def delete_by_thread(self, thread_id: str) -> int:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(self._model).where(self._model.thread_id == thread_id)
            )
            return int(result.rowcount or 0)


class SQLAlchemyCustomEventStore(_SQLAlchemyStore):
    """:class:`CustomEventStore` over a model built with ``CustomEventMixin``."""

    async def save(self, thread_id: str, run_id: str, name: str, value: Any) -> None:
        async with self._session_factory() as session, session.begin():
            session.add(
                self._model(
                    thread_id=thread_id,
                    run_id=run_id,
                    name=name,
                    value_json=store_json(value),
                )
            )

    async def list_by_thread(self, thread_id: str) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(self._model)
                    .where(self._model.thread_id == thread_id)
                    .order_by(self._model.id)
                )
            ).scalars()
            return [
                {**record_to_dict(row), "name": row.name, "value": load_json(row.value_json)}
                for row in rows
            ]


@runtime_checkable
class HistoryArchive(Protocol):
    """Durable coalesced frames for a finished run.

    Written once when a run settles. Token-level deltas stay on the hot log;
    this is what ``GET /api/v1/threads/{id}/frames`` reads after Redis has forgotten
    them.
    """

    async def save_run(
        self,
        thread_id: str,
        run_id: str,
        events: Sequence[Mapping[str, Any]],
        *,
        user_id: str | None = None,
    ) -> None: ...

    async def list_thread(
        self, thread_id: str, *, user_id: str | None = None
    ) -> list[tuple[str, list[dict[str, Any]]]]: ...

    async def delete_by_thread(self, thread_id: str) -> int: ...


class SQLAlchemyHistoryArchive(_SQLAlchemyStore):
    """:class:`HistoryArchive` over a model built with ``RunArchiveMixin``."""

    async def save_run(
        self,
        thread_id: str,
        run_id: str,
        events: Sequence[Mapping[str, Any]],
        *,
        user_id: str | None = None,
    ) -> None:
        payload = store_json(list(events))
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(select(self._model).where(self._model.run_id == run_id))
            if row is None:
                session.add(
                    self._model(
                        thread_id=thread_id,
                        run_id=run_id,
                        user_id=user_id,
                        events_json=payload,
                    )
                )
                return
            row.thread_id = thread_id
            row.user_id = user_id
            row.events_json = payload

    async def list_thread(
        self, thread_id: str, *, user_id: str | None = None
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        async with self._session_factory() as session:
            query = select(self._model).where(self._model.thread_id == thread_id)
            if user_id is not None:
                query = query.where(self._model.user_id == user_id)
            rows = (await session.execute(query.order_by(self._model.id))).scalars()
            out: list[tuple[str, list[dict[str, Any]]]] = []
            for row in rows:
                loaded = load_json(row.events_json)
                events = loaded if isinstance(loaded, list) else []
                out.append((row.run_id, events))
            return out


class InMemoryHistoryArchive:
    """A :class:`HistoryArchive` with no database, for tests."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    async def save_run(
        self,
        thread_id: str,
        run_id: str,
        events: Sequence[Mapping[str, Any]],
        *,
        user_id: str | None = None,
    ) -> None:
        payload = {
            "threadId": thread_id,
            "runId": run_id,
            "userId": user_id,
            "events": [dict(event) for event in events],
        }
        self._rows = [row for row in self._rows if row["runId"] != run_id]
        self._rows.append(payload)

    async def list_thread(
        self, thread_id: str, *, user_id: str | None = None
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        out: list[tuple[str, list[dict[str, Any]]]] = []
        for row in self._rows:
            if row["threadId"] != thread_id:
                continue
            if user_id is not None and row.get("userId") != user_id:
                continue
            out.append((row["runId"], list(row["events"])))
        return out

    async def delete_by_thread(self, thread_id: str) -> int:
        before = len(self._rows)
        self._rows = [row for row in self._rows if row["threadId"] != thread_id]
        return before - len(self._rows)


class InMemoryCustomEventStore:
    """A :class:`CustomEventStore` with no database, for tests and demos."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    async def save(self, thread_id: str, run_id: str, name: str, value: Any) -> None:
        self._rows.append({"threadId": thread_id, "runId": run_id, "name": name, "value": value})

    async def list_by_thread(self, thread_id: str) -> list[dict[str, Any]]:
        return [row for row in self._rows if row["threadId"] == thread_id]

    async def delete_by_thread(self, thread_id: str) -> int:
        before = len(self._rows)
        self._rows = [row for row in self._rows if row["threadId"] != thread_id]
        return before - len(self._rows)


__all__ = [
    "CustomEventStore",
    "HistoryArchive",
    "InMemoryCustomEventStore",
    "InMemoryHistoryArchive",
    "SQLAlchemyCustomEventStore",
    "SQLAlchemyHistoryArchive",
]
