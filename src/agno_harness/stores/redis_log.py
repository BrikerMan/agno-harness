"""RedisRunEventLog — both protocols, because a Stream is already both.

Redis Streams fit this problem almost exactly. ``XADD`` appends and returns a
monotonic id, which is the offset a client resumes from — no separate sequence
column, no gap between "stored" and "addressable". ``XREAD BLOCK`` waits for the
next entry instead of polling, which is the whole reason a live tail is possible
here and not over SQL.

Alongside the stream, per run: a hash of status and ownership, a sorted set per
thread so a thread's runs can be listed by recency, and a heartbeat key with a
short TTL. The heartbeat is how a run whose process was killed is told apart
from a run that is merely quiet — without it a client waits forever for frames
from a process that no longer exists.

TTLs mean this is a hot layer, not an archive. That is why frame-based history
requires a durable log as well: were Redis the only store, history would vanish
on eviction day, silently, while the answer is still sitting in Agno's session.

``redis`` is an optional dependency; importing this module without it raises
with an instruction rather than a traceback.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ..core.log import Frame, FrameKind, RunRecord, RunStatus, select_frames

DEFAULT_TTL_SECONDS = 24 * 3600
DEFAULT_HEARTBEAT_TTL = 30


def _require_redis() -> Any:
    try:
        from redis import asyncio as redis_asyncio
    except ImportError as exc:  # pragma: no cover - exercised by not installing it
        raise ImportError(
            "RedisRunEventLog needs the redis package: pip install 'agno-harness[redis]'"
        ) from exc
    return redis_asyncio


class RedisRunEventLog:
    """A :class:`RunEventLog` and :class:`RunEventStream` over Redis Streams.

    Parameters
    ----------
    client:
        An ``redis.asyncio.Redis``. Use :meth:`from_url` for the common case.
    namespace:
        Key prefix, so several applications can share one Redis.
    ttl_seconds:
        How long a finished run's frames stay readable. This is a cache horizon,
        not a retention policy.
    heartbeat_ttl:
        How long a run may go without a heartbeat before it is presumed dead.
    """

    def __init__(
        self,
        client: Any,
        *,
        namespace: str = "agui",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        heartbeat_ttl: int = DEFAULT_HEARTBEAT_TTL,
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.ttl_seconds = ttl_seconds
        self.heartbeat_ttl = heartbeat_ttl

    #: A cache with a TTL is not an archive. Frame-based history therefore needs
    #: a durable log beside this one, or old conversations vanish on eviction
    #: day while the answers are still sitting in Agno's session.
    is_durable = False

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisRunEventLog:
        redis_asyncio = _require_redis()
        redis_kw_names = {
            "protocol",
            "max_connections",
            "socket_connect_timeout",
            "socket_timeout",
            "socket_keepalive",
            "retry_on_timeout",
            "health_check_interval",
            "ssl",
            "ssl_cert_reqs",
            "ssl_ca_certs",
            "encoding",
        }
        redis_kwargs = {k: kwargs.pop(k) for k in list(kwargs.keys()) if k in redis_kw_names}
        if "protocol" not in redis_kwargs:
            redis_kwargs["protocol"] = 2
        return cls(redis_asyncio.from_url(url, decode_responses=True, **redis_kwargs), **kwargs)

    # ── keys ──────────────────────────────────────────────────────────────

    def _stream_key(self, run_id: str) -> str:
        return f"{self.namespace}:run:{run_id}:frames"

    def _meta_key(self, run_id: str) -> str:
        return f"{self.namespace}:run:{run_id}:meta"

    def _beat_key(self, run_id: str) -> str:
        return f"{self.namespace}:run:{run_id}:beat"

    def _thread_key(self, thread_id: str) -> str:
        return f"{self.namespace}:thread:{thread_id}:runs"

    # ── RunEventLog ───────────────────────────────────────────────────────

    async def append(self, run_id: str, frames: Sequence[Mapping[str, Any]]) -> str:
        offset = ""
        key = self._stream_key(run_id)
        pipe = self.client.pipeline()
        for event in frames:
            pipe.xadd(
                key,
                {
                    "kind": FrameKind.DELTA.value,
                    "event": json.dumps(event, ensure_ascii=False, default=str),
                },
            )
        pipe.expire(key, self.ttl_seconds)
        results = await pipe.execute()
        for value in results[:-1]:
            offset = _text(value)
        return offset

    async def read(self, run_id: str, *, after: str | None = None) -> list[Frame]:
        start = _exclusive(after) if after else "-"
        entries = await self.client.xrange(self._stream_key(run_id), min=start, max="+")
        return select_frames([_to_frame(entry) for entry in entries])

    async def start_run(
        self, run_id: str, thread_id: str, *, user_id: str | None = None, **meta: Any
    ) -> RunRecord:
        existing = await self.get_run(run_id)
        if existing is not None and existing.is_open:
            return existing
        now = time.time()
        if existing is not None:
            await self.client.delete(self._stream_key(run_id))
        record = RunRecord(
            run_id=run_id,
            thread_id=thread_id,
            user_id=user_id if user_id is not None else (existing.user_id if existing else None),
            status=RunStatus.RUNNING,
            started_at=now,
            updated_at=now,
            meta=dict(meta),
        )
        pipe = self.client.pipeline()
        pipe.hset(self._meta_key(run_id), mapping=_to_hash(record))
        pipe.expire(self._meta_key(run_id), self.ttl_seconds)
        pipe.zadd(self._thread_key(thread_id), {run_id: now})
        pipe.expire(self._thread_key(thread_id), self.ttl_seconds)
        pipe.set(self._beat_key(run_id), "1", ex=self.heartbeat_ttl)
        await pipe.execute()
        return record

    async def set_status(self, run_id: str, status: RunStatus, **meta: Any) -> None:
        update: dict[str, str] = {"status": status.value, "updated_at": str(time.time())}
        if "error" in meta:
            update["error"] = str(meta.pop("error"))
        if "unrecordable" in meta:
            update["unrecordable"] = "1" if meta.pop("unrecordable") else "0"
        if meta:
            update["meta"] = json.dumps(meta, ensure_ascii=False, default=str)
        pipe = self.client.pipeline()
        pipe.hset(self._meta_key(run_id), mapping=update)
        if status not in (RunStatus.RUNNING, RunStatus.PAUSED):
            # The run is over; stop claiming its process is alive so a tail can
            # tell "finished" from "still going".
            pipe.delete(self._beat_key(run_id))
        await pipe.execute()

    async def get_run(self, run_id: str) -> RunRecord | None:
        raw = await self.client.hgetall(self._meta_key(run_id))
        if not raw:
            return None
        record = _from_hash({_text(k): _text(v) for k, v in raw.items()})
        if record.status is RunStatus.RUNNING and not await self.client.exists(
            self._beat_key(run_id)
        ):
            # The heartbeat lapsed, so whoever was driving this run is gone.
            # Reporting it as still running would leave a client waiting on a
            # dead process. Paused runs are exempt: nothing is driving them by
            # definition, and the absent heartbeat is the expected state rather
            # than evidence of a crash.
            record.status = RunStatus.ERROR
            record.error = record.error or "the process running this run stopped responding"
        return record

    async def list_runs(self, thread_id: str, *, user_id: str | None = None) -> list[RunRecord]:
        run_ids = await self.client.zrange(self._thread_key(thread_id), 0, -1)
        records = []
        for run_id in run_ids:
            record = await self.get_run(_text(run_id))
            if record is None:
                continue
            if user_id is not None and record.user_id != user_id:
                continue
            records.append(record)
        return records

    async def heartbeat(self, run_id: str) -> None:
        await self.client.set(self._beat_key(run_id), "1", ex=self.heartbeat_ttl)

    async def delete_thread(self, thread_id: str) -> int:
        run_ids = [_text(r) for r in await self.client.zrange(self._thread_key(thread_id), 0, -1)]
        pipe = self.client.pipeline()
        for run_id in run_ids:
            pipe.delete(self._stream_key(run_id), self._meta_key(run_id), self._beat_key(run_id))
        pipe.delete(self._thread_key(thread_id))
        await pipe.execute()
        return len(run_ids)

    # ── RunEventStream ────────────────────────────────────────────────────

    async def tail(
        self, run_id: str, *, after: str | None = None, block_ms: int = 1000
    ) -> AsyncIterator[Frame]:
        cursor = after or "0-0"
        key = self._stream_key(run_id)
        while True:
            response = await self.client.xread({key: cursor}, count=100, block=block_ms)
            if response:
                for _, entries in response:
                    for entry in entries:
                        frame = _to_frame(entry)
                        cursor = frame.offset
                        yield frame
                continue
            # A timeout is the only place it is safe to check for the end: with
            # entries still arriving there may be more behind them, and stopping
            # on a status change would truncate the final frames.
            record = await self.get_run(run_id)
            if record is None or not record.is_producing:
                return


def _to_frame(entry: Any) -> Frame:
    offset, fields = entry
    data = {_text(k): _text(v) for k, v in fields.items()}
    return Frame(
        offset=_text(offset),
        event=json.loads(data.get("event") or "{}"),
        kind=FrameKind(data.get("kind") or FrameKind.DELTA.value),
    )


def _to_hash(record: RunRecord) -> dict[str, str]:
    return {
        "run_id": record.run_id,
        "thread_id": record.thread_id,
        "user_id": record.user_id or "",
        "status": record.status.value,
        "started_at": str(record.started_at),
        "updated_at": str(record.updated_at),
        "unrecordable": "1" if record.unrecordable else "0",
        "error": record.error or "",
        "meta": json.dumps(record.meta, ensure_ascii=False, default=str),
    }


def _from_hash(raw: Mapping[str, str]) -> RunRecord:
    return RunRecord(
        run_id=raw.get("run_id", ""),
        thread_id=raw.get("thread_id", ""),
        user_id=raw.get("user_id") or None,
        status=RunStatus(raw.get("status") or RunStatus.RUNNING.value),
        started_at=_float(raw.get("started_at")),
        updated_at=_float(raw.get("updated_at")),
        unrecordable=raw.get("unrecordable") == "1",
        error=raw.get("error") or None,
        meta=json.loads(raw.get("meta") or "{}"),
    )


def _exclusive(offset: str) -> str:
    """Redis ranges are inclusive; ``(id`` makes them exclusive."""
    return f"({offset}"


def _float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


__all__ = ["DEFAULT_HEARTBEAT_TTL", "DEFAULT_TTL_SECONDS", "RedisRunEventLog"]
