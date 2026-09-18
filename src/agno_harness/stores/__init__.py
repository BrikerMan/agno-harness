"""stores — where the toolbox keeps what Agno does not.

Agno persists a session so it can be fed back to the model. That leaves out
everything a *user interface* needs to redraw a conversation: the ``CUSTOM``
events a pre-run hook injected, the generative-UI blocks lifted out of the text,
and a coalesced copy of the frames so cards and sub-agent panels survive a
reload. Token-level deltas for resume live on Redis (or an in-process
stand-in), not in these SQL tables.

``RedisRunEventLog`` is not imported here — ``redis`` is optional, and importing
this package should not require it. Import it from
``agno_harness.stores.redis_log`` when you want it.
"""

from .action_store import InMemoryActionStore, SQLAlchemyActionStore
from .memory_log import InMemoryRunEventLog
from .mixins import (
    ActionRecordMixin,
    CustomEventMixin,
    MessageAuditMixin,
    RunArchiveMixin,
    RunFrameMixin,
    RunRecordMixin,
    SessionRecordMixin,
    record_to_dict,
)
from .registry import ResumeMode, StoreRegistry, Stores
from .sql_log import SQLRunEventLog
from .sql_models import (
    DefaultRelayBase,
    get_or_create_action_model,
    get_or_create_audit_model,
    get_or_create_session_model,
)
from .stores import (
    CustomEventStore,
    HistoryArchive,
    InMemoryCustomEventStore,
    InMemoryHistoryArchive,
    SQLAlchemyCustomEventStore,
    SQLAlchemyHistoryArchive,
)


def __getattr__(name: str):
    if name == "RedisRunEventLog":
        from .redis_log import RedisRunEventLog

        return RedisRunEventLog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ActionRecordMixin",
    "CustomEventMixin",
    "CustomEventStore",
    "DefaultRelayBase",
    "HistoryArchive",
    "InMemoryActionStore",
    "InMemoryCustomEventStore",
    "InMemoryHistoryArchive",
    "InMemoryRunEventLog",
    "MessageAuditMixin",
    "RedisRunEventLog",
    "ResumeMode",
    "RunArchiveMixin",
    "RunFrameMixin",
    "RunRecordMixin",
    "SQLAlchemyActionStore",
    "SQLAlchemyCustomEventStore",
    "SQLAlchemyHistoryArchive",
    "SQLRunEventLog",
    "SessionRecordMixin",
    "StoreRegistry",
    "Stores",
    "get_or_create_action_model",
    "get_or_create_audit_model",
    "get_or_create_session_model",
    "record_to_dict",
]
