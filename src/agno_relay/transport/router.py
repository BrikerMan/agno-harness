"""FastAPI transport — mount a runtime as HTTP routes.

``make_agui_router(runtime)`` gives you the AG-UI endpoint, thread history, and
the debug endpoints the frontend inspector reads::

    app.include_router(make_agui_router(runtime))

Routes
------
``POST   /agui``                       run the agent, stream AG-UI SSE
``GET    /threads``                    list threads, newest first
``GET    /threads/{id}/messages``      replay a thread
``GET    /threads/{id}/frames?after=`` replay a thread as stored frames
``DELETE /threads/{id}``               delete a thread
``GET    /debug/chunks``               recorded raw Agno chunk shapes
``GET    /debug/state/{run_id}``       translator state after a run
``GET    /debug/violations``           protocol repairs from the last run
``GET    /debug/runs/{run_id}``        everything recorded about one run

With a :class:`LongRunManager` passed in, four more, for runs that survive the
connection that started them:

``POST   /agui?long-run=1``            start detached, then attach from the log
``GET    /runs/{id}/attach?after=``    (re)attach to a run (canonical)
``GET    /runs/{id}/stream?after=``    **deprecated** alias of ``/attach``
``GET    /threads/{id}/active``        runs still going or awaiting a human
``POST   /runs/{id}/abort``            stop a run on purpose

Pass ``?debug=1`` to ``/agui`` to append a ``debug.summary`` frame with timing,
per-type frame counts and any protocol repairs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from enum import Enum
from typing import Any

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.debug import DebugTap
from ..core.log import Frame
from ..core.protocol import WIRE_PROTOCOL_VERSION
from ..runtime.inspector import violation_to_dict
from ..runtime.longrun import LongRunManager, RunNotOwned
from ..runtime.runtime import AguiRuntime
from ..runtime.threads import FramesUnavailable
from ..stores.registry import ResumeMode

# Proxies love to buffer SSE. These headers tell nginx and friends not to.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

#: Wire-level keepalive. Comment frames are ignored by SSE parsers / AG-UI
#: reducers but reset proxy idle timers (ALB/Nginx default ~60s). Aligned with
#: LangGraph Platform's ~5s heartbeat cadence.
SSE_PING_INTERVAL_S = 5.0
SSE_PING_FRAME = ": ping\n\n"

#: Tells the client what will happen if it comes back: ``none``, ``history`` or
#: ``live``. Stated rather than inferred, so a frontend does not show
#: "reconnecting…" against a server that was never going to let it reconnect.
RESUME_HEADER = "X-Agui-Resume"

#: The wire contract this server speaks. A client that checks it turns a version
#: skew into one clear error instead of a scrambled transcript.
PROTOCOL_HEADER = "X-Agui-Protocol"

logger = logging.getLogger(__name__)

UserIdResolver = Callable[[Request], str | None]


def make_agui_router(
    runtime: AguiRuntime,
    *,
    prefix: str = "",
    tags: Sequence[str | Enum] | None = None,
    resolve_user_id: UserIdResolver | None = None,
    expose_debug_routes: bool = False,
    long_runs: LongRunManager | None = None,
) -> APIRouter:
    """Build the router for one runtime.

    ``resolve_user_id`` maps a request to the identity a run is attributed to.
    It is deliberately server-side: taking the user id from the request body
    would let any caller write sessions and memories as somebody else.

    ``expose_debug_routes`` defaults to off. The chunk samples behind those
    routes are raw model output, so exposing them has to be something you asked
    for rather than something you forgot to turn off.

    ``long_runs`` adds the detach, attach and abort routes. Without it those
    routes do not exist at all, rather than existing and failing — a 404 on the
    route is a much clearer signal than a 500 halfway through a stream.
    """
    router = APIRouter(prefix=prefix, tags=list(tags or ["agui"]))
    encoder = EventEncoder()
    resume = _resume_mode(runtime, long_runs)

    def _user(request: Request) -> str | None:
        return resolve_user_id(request) if resolve_user_id else None

    if resolve_user_id is None:
        # Loud, because the failure mode is silent: without a resolver every
        # thread belongs to everyone, and nothing about the running system looks
        # wrong until somebody reads somebody else's conversation. Fine for a
        # local tool; it should be a decision, not an oversight.
        logger.warning(
            "make_agui_router() has no resolve_user_id: running in single-user mode, "
            "where every caller can read, replay and delete every thread. Pass "
            "resolve_user_id=... to attribute requests to a user."
        )

    @router.post("/agui", name="run_agent")
    async def run_agent(
        request: Request,
        run_input: RunAgentInput,
        debug: bool = Query(False, description="append a debug.summary frame"),
        long_run: bool = Query(
            False, alias="long-run", description="keep running if the client disconnects"
        ),
        long_run_underscore: bool = Query(
            False, alias="long_run", description="alias for long-run"
        ),
        detach: bool = Query(False, description="alias for long-run"),
    ) -> StreamingResponse:
        user_id = _user(request)
        headers = {
            **SSE_HEADERS,
            RESUME_HEADER: resume.value,
            PROTOCOL_HEADER: WIRE_PROTOCOL_VERSION,
        }

        is_long_run = long_run or long_run_underscore or detach
        if is_long_run and long_runs is not None:
            await long_runs.start(run_input, user_id=user_id)
            return StreamingResponse(
                _attach(long_runs, run_input.run_id, None, user_id),
                media_type="text/event-stream",
                headers=headers,
            )

        return StreamingResponse(
            _encode(runtime, run_input, request, user_id, encoder, debug),
            media_type="text/event-stream",
            headers=headers,
        )

    @router.get("/threads", name="list_threads")
    async def list_threads(request: Request) -> list[dict[str, Any]]:
        return await runtime.list_threads(user_id=_user(request))

    @router.get("/threads/{thread_id}/messages", name="thread_messages")
    async def thread_messages(request: Request, thread_id: str) -> Any:
        messages = await runtime.replay_messages(thread_id, user_id=_user(request))
        if messages is None:
            return JSONResponse(status_code=404, content={"error": "thread not found"})
        return messages

    @router.get("/threads/{thread_id}/frames", name="thread_frames")
    async def thread_frames(
        request: Request,
        thread_id: str,
        after: str | None = Query(None, description="last frame id the client holds"),
    ) -> Any:
        # The frames themselves, not messages rebuilt from a session: the client
        # feeds them to the same reducer it uses live, so a reload cannot render
        # differently from the stream it replaces.
        try:
            frames = await runtime.threads.read_frames(
                thread_id, user_id=_user(request), after=after
            )
        except FramesUnavailable as exc:
            return JSONResponse(status_code=501, content={"error": str(exc)})
        return {"frames": frames}

    @router.delete("/threads/{thread_id}", name="delete_thread")
    async def delete_thread(request: Request, thread_id: str) -> Any:
        result = await runtime.delete_thread(thread_id, user_id=_user(request))
        if result.get("error") == "thread not found":
            return JSONResponse(status_code=404, content=result)
        return result

    if long_runs is not None:

        def _attach_response(request: Request, run_id: str, after: str | None) -> StreamingResponse:
            # GET, so a browser's native EventSource can be pointed straight at
            # it — including its automatic Last-Event-ID on reconnect, which is
            # exactly the cursor this route wants.
            user_id = _user(request)
            cursor = after or request.headers.get("last-event-id")
            return StreamingResponse(
                _attach(long_runs, run_id, cursor, user_id),
                media_type="text/event-stream",
                headers={
                    **SSE_HEADERS,
                    RESUME_HEADER: resume.value,
                    PROTOCOL_HEADER: WIRE_PROTOCOL_VERSION,
                },
            )

        @router.get("/runs/{run_id}/attach", name="attach_run")
        async def attach_run(
            request: Request,
            run_id: str,
            after: str | None = Query(None, description="last offset the client holds"),
        ) -> StreamingResponse:
            """(Re)attach to an in-flight or recently finished long-run."""
            return _attach_response(request, run_id, after)

        @router.get(
            "/runs/{run_id}/stream",
            name="attach_run_stream_deprecated",
            deprecated=True,
        )
        async def attach_run_stream_deprecated(
            request: Request,
            run_id: str,
            after: str | None = Query(None, description="last offset the client holds"),
        ) -> StreamingResponse:
            """Deprecated alias of ``GET /runs/{run_id}/attach``. Prefer ``/attach``."""
            return _attach_response(request, run_id, after)

        @router.get("/threads/{thread_id}/active", name="active_runs")
        async def active_runs(request: Request, thread_id: str) -> list[dict[str, Any]]:
            user_id = _user(request)
            records = await long_runs.list_active(thread_id, user_id=user_id)
            return [_run_to_dict(record) for record in records]

        @router.post("/runs/{run_id}/abort", name="abort_run")
        async def abort_run(request: Request, run_id: str) -> Any:
            user_id = _user(request)
            try:
                aborted = await long_runs.abort(run_id, user_id=user_id)
            except RunNotOwned:
                return _not_found()
            return {"aborted": aborted}

    if expose_debug_routes:

        @router.get("/debug/chunks", name="debug_chunks")
        async def debug_chunks() -> dict[str, Any]:
            return runtime.chunk_samples()

        @router.get("/debug/state/{run_id}", name="debug_state")
        async def debug_state(run_id: str) -> Any:
            state = runtime.stream_state(run_id)
            if state is None:
                return JSONResponse(status_code=404, content={"error": "unknown run"})
            return state

        @router.get("/debug/violations", name="debug_violations")
        async def debug_violations() -> dict[str, Any]:
            return {
                "mode": runtime.sequencer_mode.value,
                "violations": [violation_to_dict(v) for v in runtime.last_violations()],
            }

        @router.get("/debug/runs/{run_id}", name="debug_run")
        async def debug_run(run_id: str) -> Any:
            # The per-run view is the accurate one: the aggregate routes above
            # answer for whichever run happened to finish last, which under
            # concurrency is not the one the caller is looking at.
            report = runtime.inspect_run(run_id)
            if report is None:
                return JSONResponse(status_code=404, content={"error": "unknown run"})
            return report

    return router


def _resume_mode(runtime: AguiRuntime, long_runs: LongRunManager | None) -> ResumeMode:
    if long_runs is None:
        return ResumeMode.NONE
    return ResumeMode.LIVE if long_runs.can_follow_live else ResumeMode.HISTORY


async def _encode(
    runtime: AguiRuntime,
    run_input: RunAgentInput,
    request: Request,
    user_id: str | None,
    encoder: EventEncoder,
    debug: bool,
) -> AsyncIterator[str]:
    async def _frames() -> AsyncIterator[str]:
        tap = DebugTap() if debug else None
        async for event in runtime.stream_events(run_input, request=request, user_id=user_id):
            if tap is not None:
                tap.observe(event)
            yield encoder.encode(event)
        if tap is not None:
            # After RUN_FINISHED on purpose: a debug frame must never be mistaken for
            # part of the run, and clients that ignore unknown CUSTOM names see
            # exactly the stream they would have got without ?debug=1.
            yield encoder.encode(tap.summary_event(runtime.last_violations()))

    async for chunk in _with_sse_ping(_frames()):
        yield chunk


async def _attach(
    long_runs: LongRunManager,
    run_id: str,
    after: str | None,
    user_id: str | None,
) -> AsyncIterator[str]:
    async def _frames() -> AsyncIterator[str]:
        try:
            async for frame in long_runs.attach(run_id, after=after, user_id=user_id):
                yield _sse(frame)
        except RunNotOwned:
            # Mid-stream is a poor place to learn this, but the alternative is
            # checking ownership twice; the frame carries the same information a
            # 404 would.
            yield (
                "data: "
                + json.dumps({"type": "RUN_ERROR", "message": "run not found"}, ensure_ascii=False)
                + "\n\n"
            )

    async for chunk in _with_sse_ping(_frames()):
        yield chunk


async def _with_sse_ping(
    source: AsyncIterator[str],
    *,
    interval_s: float = SSE_PING_INTERVAL_S,
) -> AsyncIterator[str]:
    """Yield upstream SSE chunks, emitting a comment ping at second 0 and every interval.

    Proxies and LBs drop silent streams; a ``: ping`` comment resets their idle
    timers without reaching AG-UI reducers. Emitting immediately at second 0
    warms the wire, flushes headers, and arms client watchdogs immediately.
    """
    # 0s 立即发一个 ping 打通链路
    yield SSE_PING_FRAME

    queue: asyncio.Queue[str | Exception | None] = asyncio.Queue()
    stop_event = asyncio.Event()

    async def _pinger() -> None:
        try:
            while not stop_event.is_set():
                await asyncio.sleep(interval_s)
                if stop_event.is_set():
                    break
                await queue.put(SSE_PING_FRAME)
        except asyncio.CancelledError:
            pass

    async def _reader() -> None:
        try:
            async for chunk in source:
                await queue.put(chunk)
        except Exception as exc:
            await queue.put(exc)
        finally:
            stop_event.set()
            await queue.put(None)

    pinger_task = asyncio.create_task(_pinger())
    reader_task = asyncio.create_task(_reader())

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop_event.set()
        pinger_task.cancel()
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pinger_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader_task


def _sse(frame: Frame) -> str:
    """One frame, with its offset as the SSE ``id``.

    The ``id`` is what makes resumption work without any client bookkeeping: a
    browser's EventSource replays the last one it saw as ``Last-Event-ID``.
    Clients that only read ``data:`` lines are unaffected.
    """
    return f"id: {frame.offset}\ndata: {json.dumps(frame.event, ensure_ascii=False)}\n\n"


def _run_to_dict(record: Any) -> dict[str, Any]:
    return {
        "runId": record.run_id,
        "threadId": record.thread_id,
        "status": record.status.value,
        "startedAt": record.started_at,
        "updatedAt": record.updated_at,
        "unrecordable": record.unrecordable,
        "input": (getattr(record, "meta", None) or {}).get("input") or "",
    }


def _not_found() -> JSONResponse:
    # 404 and not 403: a run id is guessable, and confirming that one exists
    # tells an attacker something they did not already know.
    return JSONResponse(status_code=404, content={"error": "run not found"})


__all__ = [
    "PROTOCOL_HEADER",
    "RESUME_HEADER",
    "SSE_HEADERS",
    "SSE_PING_FRAME",
    "SSE_PING_INTERVAL_S",
    "_with_sse_ping",
    "make_agui_router",
]
