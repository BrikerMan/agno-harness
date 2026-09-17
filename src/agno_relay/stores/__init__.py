"""stores — where the toolbox keeps what Agno does not.

Agno persists a session so it can be fed back to the model. That leaves out
everything a *user interface* needs to redraw a conversation: the ``CUSTOM``
events a pre-run hook injected, the generative-UI blocks lifted out of the text,
and a coalesced copy of the frames so cards and sub-agent panels survive a
reload. Token-level deltas for resume live on Redis (or an in-process
stand-in), not in these SQL tables.

``RedisRunEventLog`` is not imported here — ``redis`` is optional, and importing
this package should not require it. Import it from
``agno_relay.stores.redis_log`` when you want it.
"""

from .memory_log import InMemoryRunEventLog
from .mixins import CustomEventMixin, RunArchiveMixin, RunFrameMixin, RunRecordMixin, record_to_dict
from .registry import ResumeMode, StoreRegistry, Stores
from .sql_log import SQLRunEventLog
from .stores import (
    CustomEventStore,
    HistoryArchive,
    InMemoryCustomEventStore,
    InMemoryHistoryArchive,
    SQLAlchemyCustomEventStore,
    SQLAlchemyHistoryArchive,
)

__all__ = [
    "CustomEventMixin",
    "CustomEventStore",
    "HistoryArchive",
    "InMemoryCustomEventStore",
    "InMemoryHistoryArchive",
    "InMemoryRunEventLog",
    "ResumeMode",
    "RunArchiveMixin",
    "RunFrameMixin",
    "RunRecordMixin",
    "SQLAlchemyCustomEventStore",
    "SQLAlchemyHistoryArchive",
    "SQLRunEventLog",
    "StoreRegistry",
    "Stores",
    "record_to_dict",
]
