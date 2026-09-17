"""Persisting the ``CUSTOM`` events nobody else owns.

Agno stores a session so it can be replayed to the model. A ``CUSTOM`` event a
pre-run hook injected — a billing notice, a quota warning — is not part of that
conversation and so is not stored, which is correct for the model and wrong for
the user: reload the page and the notice is gone even though the charge was
real.

This module closes that gap. It watches the sequencer's output and saves every
``CUSTOM`` frame that no other module has claimed, which is the useful
definition of "an event the application invented". Frames inside a module's
namespace are left alone, because that module knows how to store and rebuild its
own — sub-agent transcripts and generative-UI blocks both need shaping that a
generic saver would get wrong.

That rule is why namespaces are enforced at assembly time. "Which module owns
this frame" has to have exactly one answer before anything can be built on it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ag_ui.core import BaseEvent, EventType

from ..module import Module
from ..scope import RunScope


class CustomEventsModule(Module):
    """Saves unclaimed ``CUSTOM`` events so replay can put them back.

    Parameters
    ----------
    stores:
        Where to save. With no ``custom_events`` store configured the module is
        inert and injected events are live-only, which is a legitimate setup
        rather than an error.
    is_claimed:
        Decides whether another module owns an event name. The runtime passes
        the registry's lookup; the default claims nothing, which is the right
        behaviour for a module used on its own.
    """

    name: str = "custom_events"
    namespace: str | None = None

    def __init__(
        self,
        *,
        stores: Any = None,
        is_claimed: Callable[[str], bool] | None = None,
    ) -> None:
        self.stores = stores
        self._is_claimed = is_claimed or (lambda _name: False)

    async def observe(self, event: BaseEvent, run: RunScope) -> None:
        if getattr(event, "type", None) is not EventType.CUSTOM:
            return
        store = getattr(self.stores, "custom_events", None)
        if store is None:
            return
        name = getattr(event, "name", "") or ""
        if not name or self._is_claimed(name):
            return
        await store.save(run.thread_id, run.run_id, name, getattr(event, "value", None))


__all__ = ["CustomEventsModule"]
