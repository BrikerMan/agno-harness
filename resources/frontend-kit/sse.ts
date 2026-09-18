/**
 * A minimal SSE reader over `fetch`, for both directions a run can be read in.
 *
 * `EventSource` cannot POST and cannot send headers, and AG-UI needs a JSON
 * body, so the stream is read from a `fetch` response instead. Frames are split
 * on the blank line that terminates an SSE event, with a carry buffer because a
 * frame can straddle two network reads.
 *
 * `id:` is parsed as well as `data:`. That line is the run's offset in the
 * event log, and holding on to the last one is the whole of resuming: reconnect
 * with `after=<id>` and the server continues from the next frame rather than
 * replaying a paragraph the user has already read.
 *
 * Wire keepalive: the server emits `: ping` comments at second 0 and every ~5s.
 * Any byte (including comments) resets the idle watchdog. After heartbeats are
 * observed, silence for ~3× the cadence aborts with `cause: "sse-idle"` so the
 * caller can re-attach — aligned with LangGraph's heartbeat-adaptive reconnect.
 */

export interface SseFrame {
  data: string;
  /** The event log offset, when the server is keeping one. */
  id?: string;
}

export interface SseOptions {
  signal?: AbortSignal;
  headers?: Record<string, string>;
  onFrame: (frame: SseFrame) => void;
  /**
   * Comment-only SSE frames (e.g. `: ping`). Not AG-UI — wire keepalive only.
   * Debug UI uses this; Events tab correctly never lists these.
   */
  onKeepalive?: () => void;
  /** Called once with the response, for headers such as `X-Agui-Resume`. */
  onResponse?: (response: Response) => void;
  /**
   * Idle watchdog. Default on. Set `false` to disable (tests).
   * When armed after the first byte, aborts if no bytes arrive for the derived
   * timeout (~3× observed ping cadence, clamped 6–30s; else 15s fallback).
   */
  idleWatchdog?: boolean;
}

const IDLE_FALLBACK_MS = 15_000;
const IDLE_MIN_MS = 6_000;
const IDLE_MAX_MS = 30_000;
const IDLE_FACTOR = 3;

export class SseIdleError extends DOMException {
  constructor(message = "SSE idle timeout: no bytes from server") {
    super(message, "AbortError");
    Object.defineProperty(this, "cause", { value: "sse-idle", enumerable: true });
  }
}

export function postSse(url: string, body: unknown, options: SseOptions): Promise<void> {
  return readSse(url, { method: "POST", body: JSON.stringify(body) }, options);
}

/**
 * Read a stream that has no body to send.
 *
 * Attaching to a run in progress is a GET on purpose: it is idempotent, it
 * survives being retried, and it is the shape a plain `EventSource` would use
 * if the demo ever needed one.
 */
export function getSse(url: string, options: SseOptions): Promise<void> {
  return readSse(url, { method: "GET" }, options);
}

async function readSse(url: string, init: RequestInit, options: SseOptions): Promise<void> {
  const response = await fetch(url, {
    ...init,
    headers: {
      Accept: "text/event-stream",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
    signal: options.signal,
  });
  options.onResponse?.(response);

  const protocol = response.headers.get("X-Agui-Protocol");
  if (protocol && protocol !== "1.0") {
    throw new Error(
      `AG-UI protocol mismatch: server sent ${protocol}, this client speaks 1.0`,
    );
  }

  if (!response.ok || !response.body) {
    const detail = await response.text().catch(() => "");
    throw new Error(`HTTP ${response.status}${detail ? `: ${detail.slice(0, 300)}` : ""}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const watchdogEnabled = options.idleWatchdog !== false;
  let lastByteAt = 0;
  let armed = false;
  let observedIntervalMs: number | null = null;
  let previousByteAt = 0;
  let idleTimer: ReturnType<typeof setInterval> | null = null;
  let idleAborted = false;

  const idleLimitMs = () => {
    if (observedIntervalMs == null) return IDLE_FALLBACK_MS;
    return Math.min(IDLE_MAX_MS, Math.max(IDLE_MIN_MS, observedIntervalMs * IDLE_FACTOR));
  };

  const noteBytes = () => {
    const now = Date.now();
    if (armed && previousByteAt > 0) {
      const gap = now - previousByteAt;
      // Treat gaps under the fallback window as heartbeat cadence samples.
      if (gap > 0 && gap < IDLE_FALLBACK_MS) {
        observedIntervalMs = observedIntervalMs == null ? gap : Math.round((observedIntervalMs + gap) / 2);
      }
    }
    previousByteAt = now;
    lastByteAt = now;
    armed = true;
  };

  if (watchdogEnabled) {
    idleTimer = setInterval(() => {
      if (!armed || idleAborted) return;
      if (Date.now() - lastByteAt < idleLimitMs()) return;
      idleAborted = true;
      try {
        reader.cancel().catch(() => {});
      } catch {
        /* ignore */
      }
    }, 1000);
  }

  try {
    for (;;) {
      if (idleAborted) throw new SseIdleError();
      if (options.signal?.aborted) {
        throw options.signal.reason instanceof Error
          ? options.signal.reason
          : new DOMException("Aborted", "AbortError");
      }
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (err) {
        if (idleAborted) throw new SseIdleError();
        throw err;
      }
      const { done, value } = chunk;
      if (done) break;
      noteBytes();
      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        emit(buffer.slice(0, boundary), options);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
    if (idleAborted) throw new SseIdleError();
    emit(buffer, options);
  } finally {
    if (idleTimer) clearInterval(idleTimer);
    reader.releaseLock();
  }
}

function emit(frame: string, options: SseOptions) {
  const parsed = parseFrame(frame);
  if (parsed) {
    options.onFrame(parsed);
    return;
  }
  // `: ping\n\n` and other comment-only blocks have no `data:` — not AG-UI.
  if (options.onKeepalive && frame.split("\n").some((line) => line.startsWith(":"))) {
    options.onKeepalive();
  }
}

function parseFrame(frame: string): SseFrame | null {
  const lines = frame.split("\n");
  const data = lines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return null;
  const idLine = lines.find((line) => line.startsWith("id:"));
  return { data, id: idLine?.slice(3).trim() || undefined };
}
