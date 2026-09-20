"""LongRunManager — a run that outlives the connection that asked for it.

WHAT CHANGES, AND WHAT DOES NOT
-------------------------------
Nothing in the runtime. ``stream_events(run_input, request=None)`` never checks
whether anybody is listening, so detaching is a matter of consuming it from a
background task instead of from a response, and writing what comes out to a log.
The manager is that task plus the bookkeeping around it.

The bookkeeping is where the interesting failures live:

*Two tabs, one run.* :meth:`start` is idempotent on ``run_id``. A second call
attaches to the run already going rather than starting a second one, because
starting twice would charge twice and interleave two models' output into one
transcript.

*Nobody is listening.* The task holds a strong reference in ``_tasks``. A bare
``create_task`` is only weakly referenced by the loop and can be garbage
collected mid-run — the run would simply stop, with no error anywhere.

*The process died.* A killed worker cannot write ``RUN_ERROR``, so a client
reconnecting to its run would wait forever on a stream nobody is producing.
Instead the manager heartbeats while pumping, and :meth:`attach` reports a run
whose heartbeat lapsed as failed. Paused runs are exempt: nothing is driving
them, so a missing beat is their normal state.

*Disconnecting is not aborting the run.* That is the point of the whole module, so
stopping needs to be something a client says out loud — :meth:`abort`.

*The server is restarting.* :meth:`shutdown` cancels what is in flight and
writes a terminal frame, so the runs end as failed rather than as ``running``
records that will be misread as orphans later.

*The log broke mid-run.* Appending failures do not stop the run. Somebody is
probably watching it; killing their output to protect a recording nobody asked
for is the wrong trade. The run is flagged ``unrecordable`` instead, which the
client learns about through ``X-Agui-Resume``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from ag_ui.core import BaseEvent, CustomEvent, EventType, RunAgentInput

from ..core.log import Frame, RunEventLog, RunEventStream, RunRecord, RunStatus, coalesce_events
from .closure import seal_session_run
from .replay import last_user_text
from .runtime import AgentRuntime
from .translator import EVENT_RUN_CANCELLED, EVENT_RUN_PAUSED

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_INTERVAL = 10.0


class LongRunError(RuntimeError):
    """Raised at assembly time, where a misconfiguration is still cheap."""


class LongRunManager:
    """Runs agents detached from the request that started them.

    Parameters
    ----------
    runtime:
        The assembled :class:`AgentRuntime`. Unmodified — the manager consumes
        its event stream like any other caller.
    log:
        Where frames go. Required: without it a detached run produces output
        into nowhere, which is worse than not detaching, because the client
        would sit waiting for a reconnection that can never deliver anything.
    stream:
        Optional live tail, which is what lets *any* process follow a run in
        progress. Defaults to ``log`` when the log can also stream. Without one,
        a run can still be followed by the process running it (see
        :meth:`attach`) but not by another worker or after a restart — the
        documented "history only, no Redis" degradation.
    heartbeat_interval:
        Seconds between liveness updates while pumping. Must be comfortably
        below the log's heartbeat TTL.
    """

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        log: RunEventLog | None = None,
        stream: RunEventStream | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        log = log if log is not None else runtime.stores.event_log
        if log is None:
            raise LongRunError(
                "LongRunManager needs a RunEventLog: a detached run has nowhere to "
                "put its output otherwise, and clients would wait forever on a "
                "reconnection that can never deliver anything. Pass log=..., or "
                "build Stores(event_log=...). Resume needs a live tail — Redis, "
                "or an in-process stand-in — not a SQL frame table."
            )
        self.runtime = runtime
        self.log = log
        if stream is None:
            stream = runtime.stores.event_stream
            if stream is None and isinstance(log, RunEventStream):
                stream = log
        self.stream = stream
        self.heartbeat_interval = heartbeat_interval
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._local = _LocalFanout()

    @property
    def can_follow_live(self) -> bool:
        """Whether *any* process can follow a run still in progress.

        Deliberately not "whether the current request can": this answers the
        ``X-Agui-Resume`` header, which is a promise about coming back later,
        possibly to a different worker. A local-only follow cannot keep that
        promise even though it works most of the time on one machine, and a
        header that is right most of the time is worse than one that is modest.
        """
        return self.stream is not None

    # ── starting ──────────────────────────────────────────────────────────

    async def start(self, run_input: RunAgentInput, *, user_id: str | None = None) -> RunRecord:
        """Begin a detached run, or return the one already going.

        Returns as soon as the run is registered, not when it finishes. The
        caller then :meth:`attach` es, and may drop the connection freely.
        """
        run_id = run_input.run_id
        thread_id = run_input.thread_id

        existing = await self.log.get_run(run_id)
        if existing is not None and existing.is_open:
            # Idempotent by run id. A reload or a double-submit must attach to
            # the run in flight; starting a second one would bill twice and
            # weave two models' output into a single transcript.
            _require_owner(existing, user_id)
            return existing

        prompt = last_user_text(run_input.messages)
        record = await self.log.start_run(
            run_id, thread_id, user_id=user_id, **({"input": prompt} if prompt else {})
        )
        with contextlib.suppress(Exception):
            for old in await self.log.list_runs(thread_id, user_id=user_id):
                if old.run_id != run_id and old.status is RunStatus.PAUSED:
                    await self.log.set_status(old.run_id, RunStatus.FINISHED)

        # Opened here rather than inside the pump: the caller attaches straight
        # after this returns, and the pump's first line has not run yet.
        self._local.open(run_id)
        task = asyncio.create_task(self._pump(run_input, user_id), name=f"agui-run-{run_id}")
        # Held deliberately: the event loop keeps only a weak reference, so a
        # task nobody stores can be collected mid-run and the run just stops.
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return record

    async def _pump(self, run_input: RunAgentInput, user_id: str | None) -> None:
        run_id = run_input.run_id
        status = RunStatus.FINISHED
        error: str | None = None
        beat = _Heartbeat(self.log, run_id, self.heartbeat_interval)
        beat_task = asyncio.create_task(beat.run(), name=f"agui-beat-{run_id}")

        try:
            async for event in self.runtime.stream_events(run_input, user_id=user_id):
                await self._append(run_id, event)
                # The status follows the frames rather than the exceptions. The
                # runtime already turns a failed agent into a RUN_ERROR frame
                # instead of raising, so reading it here is what keeps the
                # record agreeing with what the client was actually sent.
                if event.type is EventType.RUN_ERROR:
                    status, error = RunStatus.ERROR, getattr(event, "message", None)
                elif (
                    event.type is EventType.CUSTOM
                    and getattr(event, "name", "") == EVENT_RUN_PAUSED
                ):
                    # A run waiting on a human is not finished, and must not be
                    # reported as such: the client needs to find it again to
                    # answer, and Agno's side stays locked until it does.
                    status = RunStatus.PAUSED
                elif (
                    event.type is EventType.CUSTOM
                    and getattr(event, "name", "") == EVENT_RUN_CANCELLED
                ):
                    status = RunStatus.ABORTED
        except asyncio.CancelledError:
            status = RunStatus.ABORTED
            with contextlib.suppress(Exception):
                cancel_event = CustomEvent(
                    type=EventType.CUSTOM,
                    name=EVENT_RUN_CANCELLED,
                    value={"reason": "user_aborted"},
                )
                await asyncio.shield(self._append(run_id, cancel_event))
            raise
        except Exception as exc:  # noqa: BLE001 - the run's failure is data, not a crash
            logger.exception("detached run %s failed", run_id)
            status, error = RunStatus.ERROR, str(exc)
        finally:
            beat.stop()
            beat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beat_task
            with contextlib.suppress(Exception):
                await self.log.set_status(run_id, status, **({"error": error} if error else {}))
            with contextlib.suppress(Exception):
                await self._archive_run(run_id)
            # After the status, so a follower that wakes on the close reads the
            # finished record rather than a stale "running" one.
            self._local.close(run_id)

    async def _append(self, run_id: str, event: BaseEvent) -> None:
        payload = _to_frame_event(event)
        offset = ""
        try:
            offset = await self.log.append(run_id, [payload])
        except Exception:  # noqa: BLE001 - fail open, see the module docstring
            logger.warning("could not record a frame for run %s", run_id, exc_info=True)
            with contextlib.suppress(Exception):
                await self.log.set_status(run_id, RunStatus.RUNNING, unrecordable=True)
        # Published either way: whoever is watching should keep seeing the run
        # even when the recording of it is broken. Their cursor stops advancing,
        # which is the honest consequence — there is nothing to come back to.
        self._local.publish(run_id, Frame(offset=offset, event=payload))

    async def _archive_run(self, run_id: str) -> None:
        """Fold the hot log into the durable archive once the run has settled.

        Token-level deltas stay on Redis (or the in-process stand-in) for
        resume. The archive is what a reload reduces tomorrow.
        """
        archive = self.runtime.stores.history_archive
        if archive is None:
            return
        record = await self.log.get_run(run_id)
        if record is None:
            return
        events = coalesce_events([frame.event for frame in await self.log.read(run_id)])
        await archive.save_run(record.thread_id, run_id, events, user_id=record.user_id)

    # ── attaching ─────────────────────────────────────────────────────────

    async def attach(
        self, run_id: str, *, after: str | None = None, user_id: str | None = None
    ) -> AsyncIterator[Frame]:
        """Yield a run's frames from ``after`` onwards, following it if it can.

        Passing no ``after`` replays from the beginning, which is what a fresh
        page load wants; passing the last offset it saw is what a client with a
        half-drawn transcript wants.

        Three cases, in decreasing order of capability:

        *With a stream* a still-producing run is followed from any process,
        which is the case that makes ``X-Agui-Resume: live`` truthful. A paused
        run is not producing: yield what is stored and end, so the client can
        show the form instead of hanging on a live tail.

        *Without one, but driven here*, the frames are still going past in this
        process on their way to the log, so they are handed to the caller as
        well. This is what makes a detached run watchable on a server with no
        Redis, and it costs nothing: no polling, and the log is untouched.

        *Otherwise* — another worker's run, or one that is already over — the
        stored frames are returned and the stream ends. Ending matters: a client
        holding a connection that will never produce another byte is worse off
        than one told plainly that there is no more to come.
        """
        record = await self.log.get_run(run_id)
        if record is None:
            return
        _require_owner(record, user_id)

        if self.stream is not None and record.is_producing:
            async for frame in self.stream.tail(run_id, after=after):
                yield frame
            return

        # Subscribed before the history is read, or frames written between the
        # read and the subscription would fall down the gap between them.
        # ``None`` means nothing here is driving this run — another worker's, or
        # one that ended — and all this call can do is return what is stored.
        live = self._local.subscribe(run_id)
        try:
            cursor = after
            for frame in await self.log.read(run_id, after=after):
                cursor = frame.offset
                yield frame
            if live is None:
                return
            while (followed := await live.get()) is not None:
                # Offsets sort lexically on every backend, so this drops the
                # overlap between what the history already contained and what
                # arrived while it was being read.
                if cursor is not None and followed.offset <= cursor:
                    continue
                cursor = followed.offset or cursor
                yield followed
        finally:
            if live is not None:
                self._local.unsubscribe(run_id, live)

    async def list_active(self, thread_id: str, *, user_id: str | None = None) -> list[RunRecord]:
        """Runs in this thread that are still running or awaiting a human."""
        runs = await self.log.list_runs(thread_id, user_id=user_id)
        return [record for record in runs if record.is_open]

    # ── stopping ──────────────────────────────────────────────────────────

    async def abort(self, run_id: str, *, user_id: str | None = None) -> bool:
        """Stop a run on purpose. Returns whether there was one to stop.

        Needed precisely because closing the connection no longer does this.
        """
        record = await self.log.get_run(run_id)
        if record is None:
            return False
        # Before the liveness check, not after: whether a stranger is refused
        # should not depend on how far along the run happens to be.
        _require_owner(record, user_id)
        if not record.is_open:
            return False

        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        else:
            with contextlib.suppress(Exception):
                cancel_event = CustomEvent(
                    type=EventType.CUSTOM,
                    name=EVENT_RUN_CANCELLED,
                    value={"reason": "user_aborted"},
                )
                await self._append(run_id, cancel_event)
                await self._archive_run(run_id)
        await self.log.set_status(run_id, RunStatus.ABORTED)
        target_db = self.runtime.db or getattr(self.runtime.agent, "db", None)
        if target_db is not None:
            with contextlib.suppress(Exception):
                await seal_session_run(
                    target_db,
                    session_id=record.thread_id,
                    run_id=run_id,
                    reason="Aborted by user",
                    is_error=False,
                )
        return True

    async def shutdown(self) -> None:
        """Stop every in-flight run and mark it, for a clean restart.

        Left alone, these records stay ``running`` in a log that outlives the
        process, and every client that reconnects waits on a task that no
        longer exists.
        """
        for run_id, task in list(self._tasks.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            with contextlib.suppress(Exception):
                await self.log.set_status(
                    run_id, RunStatus.ERROR, error="the server restarted while this run was going"
                )
        self._tasks.clear()


class _LocalFanout:
    """Hands frames to whoever is watching a run this process is driving.

    The queues are unbounded on purpose. A bounded one would force a choice
    between blocking the run because a reader is slow and dropping frames from
    a transcript, and both are worse than briefly holding a run's worth of
    events in memory — which is the same order of magnitude as the answer being
    written.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[Frame | None]]] = {}

    def open(self, run_id: str) -> None:
        self._subscribers.setdefault(run_id, set())

    def subscribe(self, run_id: str) -> asyncio.Queue[Frame | None] | None:
        """A queue of this run's remaining frames, or ``None`` if it is not ours.

        Membership is the test for "driven here", and it is checked and mutated
        without awaiting anywhere, so a run cannot finish between the check and
        the subscription and leave a watcher waiting on a sentinel that was
        already sent.
        """
        watchers = self._subscribers.get(run_id)
        if watchers is None:
            return None
        queue: asyncio.Queue[Frame | None] = asyncio.Queue()
        watchers.add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[Frame | None]) -> None:
        watchers = self._subscribers.get(run_id)
        if watchers is not None:
            watchers.discard(queue)

    def publish(self, run_id: str, frame: Frame) -> None:
        for queue in self._subscribers.get(run_id, ()):
            queue.put_nowait(frame)

    def close(self, run_id: str) -> None:
        """Tell every watcher the run is over, so none of them waits forever."""
        for queue in self._subscribers.pop(run_id, ()):
            queue.put_nowait(None)


class _Heartbeat:
    """Sends background heartbeats to the log periodically while active."""

    def __init__(self, log: RunEventLog, run_id: str, interval: float) -> None:
        self._log = log
        self._run_id = run_id
        self._interval = interval
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        with contextlib.suppress(Exception):
            await self._log.heartbeat(self._run_id)
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            if self._stop_event.is_set():
                break
            with contextlib.suppress(Exception):
                await self._log.heartbeat(self._run_id)

    async def maybe(self) -> None:
        with contextlib.suppress(Exception):
            await self._log.heartbeat(self._run_id)


class RunNotOwned(PermissionError):
    """Raised when a run belongs to somebody else.

    The transport turns this into a 404 rather than a 403: a run id is
    guessable, and answering "yes, that exists, but not for you" tells an
    attacker something they did not know.
    """


def _require_owner(record: RunRecord, user_id: str | None) -> None:
    if record.user_id is not None and record.user_id != user_id:
        raise RunNotOwned(record.run_id)


def _to_frame_event(event: BaseEvent) -> Mapping[str, Any]:
    """The event as the client would have received it, not a summary of it.

    Matching ``EventEncoder``'s ``by_alias`` and ``exclude_none`` matters: a
    replayed frame has to be byte-identical to the live one, or the frontend
    ends up needing two code paths for what is meant to be the same event.
    """
    return event.model_dump(by_alias=True, exclude_none=True, mode="json")


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "LongRunError",
    "LongRunManager",
    "RunNotOwned",
]
