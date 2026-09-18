"""StoreRegistry — bind your models to the records the toolbox persists.

::

    from agno_harness.persistence import (
        CustomEventMixin, CustomEventStore, StoreRegistry,
    )

    class AgentCustomEvent(Base, CustomEventMixin):
        __tablename__ = "agent_custom_events"

    registry = StoreRegistry()
    registry.register(CustomEventStore, AgentCustomEvent)
    stores = registry.build(session_factory)

    runtime = AgentRuntime(agent=agent, db=db, stores=stores)

Registering nothing is fine: the runtime treats a missing store as "do not
persist this", so injected events stay live-only and everything else still
works.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..core.log import RunEventLog, RunEventStream
from .sql_log import SQLRunEventLog
from .stores import (
    CustomEventStore,
    HistoryArchive,
    SQLAlchemyCustomEventStore,
    SQLAlchemyHistoryArchive,
)

# Which SQLAlchemy store implementation backs each record kind.
_IMPLEMENTATIONS: dict[Any, type[Any]] = {
    CustomEventStore: SQLAlchemyCustomEventStore,
    HistoryArchive: SQLAlchemyHistoryArchive,
}


class ResumeMode(StrEnum):
    """What a client may expect if it comes back, sent as ``X-Agui-Resume``.

    Stated in a header rather than inferred, because the three cases look
    identical from the outside until the client tries — and a client that
    guesses wrong shows "reconnecting…" forever against a backend that was never
    going to let it reconnect.
    """

    #: No log. The run lives on the connection and dies with it.
    NONE = "none"
    #: Durable log, no live tail. Reload shows what has been written; a run in
    #: progress cannot be followed. SQL frame logs still advertise this; prefer
    #: Redis (or an in-process stand-in) for resume, and a history archive for
    #: reload.
    HISTORY = "history"
    #: Durable log plus a live tail. Reconnect resumes mid-sentence.
    LIVE = "live"


class Stores:
    """The built store set handed to the runtime. Any member may be ``None``.

    ``event_log`` and ``event_stream`` are separate on purpose, and may be the
    same object. Wanting durability *and* liveness from backends that each do
    one is a legitimate configuration: pass the SQL log as ``event_log`` and the
    Redis one as ``event_stream``, and both get written.
    """

    def __init__(
        self,
        custom_events: CustomEventStore | None = None,
        event_log: RunEventLog | None = None,
        event_stream: RunEventStream | None = None,
        history_archive: HistoryArchive | None = None,
    ) -> None:
        self.custom_events = custom_events
        self.event_log = event_log
        # A log that can also tail is its own stream; saying so twice at every
        # call site is how one of the two ends up forgotten.
        if event_stream is None and isinstance(event_log, RunEventStream):
            event_stream = event_log
        self.event_stream = event_stream
        self.history_archive = history_archive

    @property
    def resume_mode(self) -> ResumeMode:
        """Resume is a live tail. A SQL frame log is not one.

        ``HISTORY`` remains for a durable log with no stream, which can finish a
        detached run but cannot follow it. New wiring uses Redis (or memory) as
        the stream and a history archive for reload.
        """
        if self.event_stream is not None:
            return ResumeMode.LIVE
        if self.event_log is not None:
            return ResumeMode.HISTORY
        return ResumeMode.NONE

    @property
    def is_durable(self) -> bool:
        """Whether history survives eviction of the hot log.

        Redis has a TTL. Frame replay after that horizon needs a history
        archive (or a durable SQL event log).
        """
        if self.history_archive is not None:
            return True
        log = self.event_log
        return log is not None and getattr(log, "is_durable", True)

    @classmethod
    def in_memory(cls) -> Stores:
        """Create a fully functional in-memory store set for testing and local development.

        Uses ``InMemoryRunEventLog``, ``InMemoryCustomEventStore``, and ``InMemoryHistoryArchive``,
        enabling live tailing, detach/attach, and history replay without external databases.
        """
        from .memory_log import InMemoryRunEventLog
        from .stores import InMemoryCustomEventStore, InMemoryHistoryArchive

        log = InMemoryRunEventLog()
        return cls(
            custom_events=InMemoryCustomEventStore(),
            event_log=log,
            event_stream=log,
            history_archive=InMemoryHistoryArchive(),
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        names = ("custom_events", "event_log", "event_stream", "history_archive")
        present = [n for n in names if getattr(self, n) is not None]
        return f"Stores({', '.join(present) or 'empty'})"


class StoreRegistry:
    """Maps record kinds to your mapped classes, or to ready-made instances."""

    def __init__(self) -> None:
        self._models: dict[Any, type[Any]] = {}
        self._instances: dict[Any, Any] = {}
        self._event_log_models: tuple[type[Any], type[Any]] | None = None

    def register(self, kind: Any, model: type[Any]) -> StoreRegistry:
        """Bind a record kind to one of your SQLAlchemy models."""
        if kind not in _IMPLEMENTATIONS:
            raise ValueError(
                f"Unknown store kind {kind!r}. "
                f"Expected one of: {', '.join(k.__name__ for k in _IMPLEMENTATIONS)}"
            )
        _require_columns(kind, model)
        self._models[kind] = model
        return self

    def register_event_log(self, frame_model: type[Any], run_model: type[Any]) -> StoreRegistry:
        """Bind the event log to your frame and run-record models.

        Separate from :meth:`register` because the log spans two tables: the
        frames themselves and the per-run status that makes them findable.
        """
        _require_columns_on(frame_model, ("thread_id", "run_id", "sequence", "kind", "event_json"))
        _require_columns_on(run_model, ("thread_id", "run_id", "user_id", "status"))
        self._event_log_models = (frame_model, run_model)
        return self

    def register_instance(self, kind: Any, store: Any) -> StoreRegistry:
        """Bind a record kind to your own store object, bypassing SQLAlchemy."""
        self._instances[kind] = store
        return self

    def build(self, session_factory: async_sessionmaker[Any] | None = None) -> Stores:
        """Instantiate the registered stores.

        ``session_factory`` is only needed for kinds registered via
        :meth:`register`; instances registered directly are passed through.
        """
        built: dict[Any, Any] = dict(self._instances)
        for kind, model in self._models.items():
            if session_factory is None:
                raise ValueError(
                    f"{kind.__name__} was registered with model {model.__name__}, "
                    "so build() needs a session_factory"
                )
            built[kind] = _IMPLEMENTATIONS[kind](session_factory, model)
        if self._event_log_models is not None and RunEventLog not in built:
            if session_factory is None:
                raise ValueError(
                    "register_event_log() was used, so build() needs a session_factory"
                )
            built[RunEventLog] = SQLRunEventLog(session_factory, *self._event_log_models)
        return Stores(
            custom_events=built.get(CustomEventStore),
            event_log=built.get(RunEventLog),
            event_stream=built.get(RunEventStream),
            history_archive=built.get(HistoryArchive),
        )


_REQUIRED_COLUMNS: dict[Any, tuple[str, ...]] = {
    CustomEventStore: ("thread_id", "run_id", "name", "value_json"),
    HistoryArchive: ("thread_id", "run_id", "events_json"),
}


def _require_columns(kind: Any, model: type[Any]) -> None:
    """Fail at registration rather than at the first write."""
    missing = [name for name in _REQUIRED_COLUMNS[kind] if not hasattr(model, name)]
    if missing:
        raise TypeError(
            f"{model.__name__} cannot back {kind.__name__}: missing column(s) "
            f"{', '.join(missing)}. Compose the matching mixin onto your model, "
            "or use register_instance() with your own store."
        )


def _require_columns_on(model: type[Any], columns: tuple[str, ...]) -> None:
    missing = [name for name in columns if not hasattr(model, name)]
    if missing:
        raise TypeError(
            f"{model.__name__} is missing column(s) {', '.join(missing)}. "
            "Compose the matching mixin onto your model."
        )


__all__ = ["ResumeMode", "StoreRegistry", "Stores"]
