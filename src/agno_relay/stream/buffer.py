import asyncio
import time
from collections.abc import Awaitable, Callable


class ThrottledStreamBuffer:
    """Buffers text deltas and periodically invokes a flush callback.

    Designed to prevent HTTP 429 rate limit errors on IM platforms
    like Microsoft Teams and Lark.
    """

    def __init__(
        self,
        flush_callback: Callable[[str], Awaitable[None]],
        min_interval_seconds: float = 1.5,
    ) -> None:
        self.flush_callback = flush_callback
        self.min_interval = min_interval_seconds
        self._buffer: list[str] = []
        self._last_flush_time = 0.0
        self._lock = asyncio.Lock()
        self._dirty = False

    async def append(self, text: str) -> None:
        """Append chunk and trigger flush if interval has elapsed."""
        if not text:
            return
        async with self._lock:
            self._buffer.append(text)
            self._dirty = True
            now = time.monotonic()
            if now - self._last_flush_time >= self.min_interval:
                await self._do_flush_locked()

    async def flush(self) -> None:
        """Force flush all buffered text."""
        async with self._lock:
            if self._dirty:
                await self._do_flush_locked()

    async def _do_flush_locked(self) -> None:
        content = "".join(self._buffer)
        self._last_flush_time = time.monotonic()
        self._dirty = False
        await self.flush_callback(content)

    @property
    def current_content(self) -> str:
        return "".join(self._buffer)
