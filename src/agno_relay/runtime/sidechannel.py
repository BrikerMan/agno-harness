"""The side channel — how a running tool gets something into the stream.

While a tool is executing, the agent's own stream is blocked awaiting the tool's
return value. Anything the tool wants to show *before* it returns — a sub-agent's
reasoning, a card of search results, a progress step — has nowhere to go through
the normal path. So it goes onto a queue, and :func:`merge_side_channel` races
that queue against the agent's stream and yields whichever is ready first.

A plain queue would not be enough: buffering the items and delivering them after
the tool returned would defeat the point, which is that the user sees them while
they wait.

Items are opaque here. A module claims the kinds it understands
(``BridgeModule.claims_item``), which is what keeps one feature's items from
becoming every feature's problem.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

_CHANNEL: ContextVar[SideChannel | None] = ContextVar("better_agno_side_channel", default=None)


@runtime_checkable
class Bracketed(Protocol):
    """An item that opens or closes a region the parent must not interrupt."""

    #: ``+1`` opens, ``-1`` closes, ``0`` is neither.
    bracket: int


class SideChannel:
    """An ``asyncio.Queue`` of out-of-band items, scoped to one run."""

    def __init__(self, *, enabled: bool = True, maxsize: int = 0) -> None:
        self.enabled = enabled
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)

    async def put(self, item: Any) -> None:
        await self._queue.put(item)

    async def get(self) -> Any:
        return await self._queue.get()

    def drain_nowait(self) -> list[Any]:
        items: list[Any] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return items


@contextlib.contextmanager
def bind_channel(channel: SideChannel | None) -> Iterator[SideChannel | None]:
    """Install ``channel`` for the current context (used by the runner).

    Do not hold this across an async generator ``yield`` to a consumer: on
    cancellation ``ContextVar.reset`` can run in a different Context and raise
    ``ValueError``. Bind only around the await that executes tools.
    """
    token = _CHANNEL.set(channel)
    try:
        yield channel
    finally:
        try:
            _CHANNEL.reset(token)
        except ValueError:
            # Token was created in another Context (async-gen cancel / athrow).
            # Clearing is best-effort; the next bind will overwrite.
            if _CHANNEL.get() is channel:
                _CHANNEL.set(None)


def current_channel() -> SideChannel | None:
    return _CHANNEL.get()


@dataclass
class ParentChunk:
    """A chunk from the parent agent's own stream."""

    chunk: Any


async def merge_side_channel(
    parent: AsyncIterator[Any], channel: SideChannel
) -> AsyncIterator[Any]:
    """Interleave the parent stream with whatever tools put on the channel.

    Yields in arrival order and stops once the parent stream is exhausted and
    the channel has been drained, with one exception: while a bracketed region
    is open, the parent's own chunks are held back so that region stays
    contiguous. Without that, whether a parent chunk lands inside the bracket
    comes down to which task ``asyncio.wait`` happens to return first, and a
    client grouping by the bracket would show the parent's prose as the
    sub-agent's. The parent is blocked on the delegating tool anyway, so there
    is normally nothing to hold.
    """
    parent_iter = parent.__aiter__()
    parent_task: asyncio.Task[Any] | None = asyncio.ensure_future(_anext(parent_iter))
    channel_task: asyncio.Task[Any] | None = asyncio.ensure_future(channel.get())
    open_brackets = 0
    held: list[Any] = []

    try:
        while parent_task is not None:
            waitables = {t for t in (parent_task, channel_task) if t is not None}
            done, _ = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)

            if channel_task is not None and channel_task in done:
                item = channel_task.result()
                channel_task = asyncio.ensure_future(channel.get())
                open_brackets = max(open_brackets + _bracket_of(item), 0)
                yield item
                if open_brackets == 0:
                    for chunk in held:
                        yield ParentChunk(chunk)
                    held.clear()

            if parent_task in done:
                sentinel = parent_task.result()
                if sentinel is _EXHAUSTED:
                    parent_task = None
                    break
                if open_brackets:
                    held.append(sentinel)
                else:
                    yield ParentChunk(sentinel)
                parent_task = asyncio.ensure_future(_anext(parent_iter))

        # The parent is done, but a tool may have queued items in the same tick.
        # Drain them before closing so nothing is silently dropped.
        for item in channel.drain_nowait():
            yield item
        for chunk in held:
            yield ParentChunk(chunk)
    finally:
        for task in (parent_task, channel_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


def _bracket_of(item: Any) -> int:
    value = getattr(item, "bracket", 0)
    return value if isinstance(value, int) else 0


class _Exhausted:
    __slots__ = ()


_EXHAUSTED = _Exhausted()


async def _anext(iterator: Any) -> Any:
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _EXHAUSTED


__all__ = [
    "Bracketed",
    "ParentChunk",
    "SideChannel",
    "bind_channel",
    "current_channel",
    "merge_side_channel",
]
