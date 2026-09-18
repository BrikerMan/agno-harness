/**
 * The AG-UI client: sends a run, reduces the event stream into messages.
 *
 * The reducer is the mirror image of the server's sequencer. Because the
 * server guarantees the stream is well-formed, this side never has to defend
 * against content before a start or an unclosed tool call — it can just apply
 * each frame.
 *
 * Copy this directory into the product. Parameterize `apiBase` / `getHeaders` /
 * `storageKey`. Do not rewrite `applyEvent`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  EMPTY_STATS,
  newAssistantMessage,
  type AguiEvent,
  type ChatMessage,
  type DebugFrame,
  type PendingTool,
  type RunStats,
  type StatePatchOp,
  type Violation,
} from "./agui";
import {
  EMPTY_STATE,
  applyEvent,
  confirmationIdsFromFrames,
  dropEmptyTail,
  fromReplayMessage,
  interleave,
  interruptOpenWork,
  mergeSessionToolResults,
  newId,
  replayFrames,
  settleAnsweredPause,
  stampToolAnswers,
  toWireMessage,
  type ChatState,
} from "./apply-event";
import { getSse, postSse } from "./sse";

/** What the server will let a returning client do. Sent as `X-Agui-Resume`. */
export type ResumeMode = "none" | "history" | "live";

const RESUME_HEADER = "X-Agui-Resume";
const RECONNECT_DELAYS_MS = [400, 1200, 3000];

/** Enough to pick a run back up: which thread, which run, how far we got. */
interface StoredSession {
  threadId: string;
  runId?: string;
  lastEventId?: string;
}

export type { DebugFrame, StatePatchOp, Violation };

export interface FrontendTool {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  execute: (args: Record<string, unknown>) => Promise<unknown>;
}

export interface SendOptions {
  /** Seed the shared-state document for this run. */
  state?: Record<string, unknown> | null;
  /** Resume a paused run instead of starting a new one. */
  toolResults?: Array<{ toolCallId: string; content: string }>;
  /** Keep the visible transcript as-is (used when resuming). */
  keepTranscript?: boolean;
  /** Opaque props forwarded to the server (e.g. reasoning level). */
  forwardedProps?: Record<string, unknown> | null;
}

export interface UseAguiChat {
  messages: ChatMessage[];
  frames: DebugFrame[];
  violations: Violation[];
  sharedState: unknown;
  statePatches: StatePatchOp[];
  stats: RunStats;
  pendingTools: PendingTool[];
  lastRequest: unknown;
  isStreaming: boolean;
  /** The connection dropped and we are trying to pick the run back up. */
  isReconnecting: boolean;
  resumeMode: ResumeMode;
  error: string | null;
  threadId: string;
  send: (text: string, options?: SendOptions) => Promise<void>;
  respondToTools: (results: Array<{ toolCallId: string; content: string }>) => Promise<void>;
  stop: () => void;
  reset: (threadId?: string) => void;
  loadThread: (threadId: string) => Promise<void>;
  clearFrames: () => void;
  registerFrontendTool: (tool: FrontendTool) => void;
}

export interface UseAguiChatOptions {
  /**
   * Origin the browser talks to, no trailing slash.
   * Default `/api` — pair with a Vite proxy that strips `/api`.
   */
  apiBase?: string;
  /** localStorage key for `{threadId, runId, lastEventId}`. */
  storageKey?: string;
  /** Identity and extra headers on every request. */
  getHeaders?: () => Record<string, string>;
  /** Append a `debug.summary` frame (`POST /agui?debug=1`). */
  debug?: boolean;
}

const emptyHeaders = (): Record<string, string> => ({});

type FetchFn = (path: string, init?: RequestInit) => Promise<Response>;

function makeFetch(apiBase: string, getHeaders: () => Record<string, string>): FetchFn {
  return (path, init = {}) =>
    fetch(`${apiBase}${path}`, {
      ...init,
      headers: { ...getHeaders(), ...((init.headers as Record<string, string>) ?? {}) },
    });
}

export function useAguiChat(options: UseAguiChatOptions = {}): UseAguiChat {
  const apiBase = options.apiBase ?? "/api";
  const storageKey = options.storageKey ?? "agui:session";
  const headersRef = useRef(options.getHeaders ?? emptyHeaders);
  headersRef.current = options.getHeaders ?? emptyHeaders;
  const request = useMemo(
    () => makeFetch(apiBase, () => headersRef.current()),
    [apiBase],
  );

  const [state, setState] = useState<ChatState>(EMPTY_STATE);
  const [frames, setFrames] = useState<DebugFrame[]>([]);
  const [stats, setStats] = useState<RunStats>(EMPTY_STATS);
  const [lastRequest, setLastRequest] = useState<unknown>(null);
  const [isStreaming, setStreaming] = useState(false);
  const [isReconnecting, setReconnecting] = useState(false);
  /** `null` until `/health` answers — `"none"` would skip restore attach. */
  const [resumeMode, setResumeMode] = useState<ResumeMode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [threadId, setThreadId] = useState(() => loadSession(storageKey)?.threadId ?? newId("thread"));

  const abortRef = useRef<AbortController | null>(null);
  const frontendTools = useRef(new Map<string, FrontendTool>());
  const runStateRef = useRef<Record<string, unknown> | null>(null);
  const activeRunRef = useRef<string | null>(null);
  const lastEventIdRef = useRef<string | null>(null);
  /** Set while the user is deliberately stopping, so a stop is not a dropout. */
  const stoppingRef = useRef(false);
  // Read at send time so opening the panel mid-run does not rebuild `run`.
  const debugRef = useRef(Boolean(options.debug));
  debugRef.current = Boolean(options.debug);
  const threadIdRef = useRef(threadId);
  threadIdRef.current = threadId;

  const registerFrontendTool = useCallback((tool: FrontendTool) => {
    frontendTools.current.set(tool.name, tool);
  }, []);

  /** One live frame: record it for the inspector, then reduce it. */
  const makeSink = useCallback((clock: RunClock) => {
    return (event: AguiEvent, id?: string) => {
      if (id) lastEventIdRef.current = id;
      const frame = clock.tick(event);
      setFrames((prev) => [...prev, frame]);
      setStats(clock.summary());
      setState((prev) => applyEvent(prev, event, frame.offsetMs));
      // A pause is a completed stream waiting on the user. Unlock the form
      // even if the SSE tail has not closed yet.
      if (event.type === "CUSTOM" && event.name === "run.paused") {
        setStreaming(false);
      }
      if (activeRunRef.current) {
        saveSession(storageKey, {
          threadId: clock.threadId,
          runId: activeRunRef.current,
          lastEventId: lastEventIdRef.current ?? undefined,
        });
      }
    };
  }, [storageKey]);

  const run = useCallback(
    async (userText: string | null, options: SendOptions = {}) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      stoppingRef.current = false;

      const runId = newId("run");
      const clock = new RunClock(threadId);

      // Build the message list the server sees. Resuming appends tool results
      // to the existing transcript; a fresh turn appends the user's text.
      const wireMessages: Array<Record<string, unknown>> = state.messages.map(toWireMessage);
      if (userText !== null) {
        const userMessage: ChatMessage = {
          ...newAssistantMessage(newId("msg")),
          role: "user",
          content: userText,
          texts: { "text-0": userText },
          order: [{ kind: "text", key: "text-0" }],
        };
        wireMessages.push(toWireMessage(userMessage));
        setState((prev) => ({ ...prev, messages: [...prev.messages, userMessage] }));
      }
      for (const result of options.toolResults ?? []) {
        wireMessages.push({
          id: newId("tool"),
          role: "tool",
          toolCallId: result.toolCallId,
          content: result.content,
        });
      }

      if (options.state !== undefined) runStateRef.current = options.state ?? null;

      const payload = {
        threadId,
        runId,
        state: runStateRef.current,
        messages: wireMessages,
        tools: [...frontendTools.current.values()].map((tool) => ({
          name: tool.name,
          description: tool.description,
          parameters: tool.parameters,
        })),
        context: [],
        forwardedProps: options.forwardedProps ?? null,
      };

      setLastRequest(payload);
      setStreaming(true);
      setError(null);
      setState((prev) => ({
        ...prev,
        pendingTools: [],
        ...(options.keepTranscript ? {} : { statePatches: [], violations: [] }),
      }));
      setStats({ ...EMPTY_STATS, startedAt: clock.startedAt });
      activeRunRef.current = runId;
      lastEventIdRef.current = null;
      saveSession(storageKey, { threadId, runId });

      const sink = makeSink(clock);
      const onKeepalive = () => {
        clock.notePing();
        setStats(clock.summary());
      };
      const params = new URLSearchParams();
      if (debugRef.current) params.set("debug", "1");
      // Long-run when the server can serve the run back: the run then belongs to
      // the log rather than to this socket, and closing the laptop lid stops
      // being the same thing as cancelling.
      if (resumeMode && resumeMode !== "none") params.set("long-run", "1");
      const query = params.toString();

      try {
        await postSse(`${apiBase}/agui${query ? `?${query}` : ""}`, payload, {
          signal: controller.signal,
          headers: headersRef.current(),
          onResponse: (response) => setResumeMode(readResumeMode(response)),
          onFrame: (frame) => deliver(frame, sink, setError),
          onKeepalive,
        });
      } catch (err) {
        if ((err as Error).name === "AbortError") {
          // Stop() ends here. A refresh aborts too — leave runId in session so
          // the next page can attach, rather than racing a reconnect as this
          // one unloads.
          if (!stoppingRef.current) await reconnect(runId, sink, setError, setReconnecting, onKeepalive);
        } else {
          const recovered = await reconnect(runId, sink, setError, setReconnecting, onKeepalive);
          if (!recovered) setError((err as Error).message);
        }
      } finally {
        setStreaming(false);
        setReconnecting(false);
        activeRunRef.current = null;
        setStats(clock.summary());
        setState((prev) => {
          const next = dropEmptyTail(prev);
          return stoppingRef.current || resumeMode !== "live" && resumeMode !== "history"
            ? interruptOpenWork(next)
            : next;
        });
        // Abort without Stop is a refresh or a dropped tab: keep runId so the
        // next load can follow the detached run. Finished and cancelled runs
        // drop it, or the next reload would try to attach to a corpse.
        // Skip if the user already switched threads — loadThread saved the new one.
        if (
          (stoppingRef.current || !controller.signal.aborted) &&
          threadIdRef.current === threadId
        ) {
          saveSession(storageKey, { threadId });
        }
      }
    },
    [apiBase, makeSink, resumeMode, state.messages, storageKey, threadId],
  );

  /**
   * Try to pick a dropped run back up from where the transcript ends.
   *
   * `after` is the last frame this client actually reduced, so the run resumes
   * mid-sentence instead of repeating a paragraph the user already read.
   *
   * Worth trying even when the server only promises `history`: if the run is
   * still being driven by the process we reach, it will be followed, and if it
   * is not, the attach returns what is stored and ends. Either beats giving up.
   */
  const reconnect = useCallback(
    async (
      runId: string,
      sink: (event: AguiEvent, id?: string) => void,
      fail: (message: string) => void,
      setBusy: (busy: boolean) => void,
      onKeepalive?: () => void,
    ): Promise<boolean> => {
      if (!resumeMode || resumeMode === "none") return false;
      setBusy(true);
      try {
        for (const delay of RECONNECT_DELAYS_MS) {
          await sleep(delay);
          const controller = new AbortController();
          abortRef.current = controller;
          const after = lastEventIdRef.current;
          try {
            await getSse(
              `${apiBase}/runs/${runId}/attach${after ? `?after=${encodeURIComponent(after)}` : ""}`,
              {
                signal: controller.signal,
                headers: headersRef.current(),
                onFrame: (frame) => deliver(frame, sink, fail),
                onKeepalive,
              },
            );
            return true;
          } catch (err) {
            if ((err as Error).name === "AbortError") return false;
          }
        }
        fail("Lost the connection to this run and could not pick it back up.");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [apiBase, resumeMode],
  );

  const send = useCallback(
    (text: string, options?: SendOptions) => run(text, options ?? {}),
    [run],
  );

  const respondToTools = useCallback(
    async (results: Array<{ toolCallId: string; content: string }>) => {
      setState((prev) => stampToolAnswers(prev, results));
      await run(null, { toolResults: results, keepTranscript: true });
    },
    [run],
  );

  /**
   * Stop the run, rather than merely stop watching it.
   *
   * Once a run is detached, closing the socket no longer ends it — the server
   * would keep going and the next reload would show it still writing. Cancel is
   * the explicit request the detach made necessary.
   */
  const stop = useCallback(() => {
    stoppingRef.current = true;
    const runId = activeRunRef.current;
    abortRef.current?.abort();
    setStreaming(false);
    setReconnecting(false);
    if (runId && resumeMode && resumeMode !== "none") {
      void request(`/runs/${runId}/abort`, { method: "POST" }).catch(() => {});
    }
  }, [request, resumeMode]);

  const reset = useCallback((next?: string) => {
    stoppingRef.current = true;
    abortRef.current?.abort();
    setState(EMPTY_STATE);
    setFrames([]);
    setStats(EMPTY_STATS);
    setError(null);
    runStateRef.current = null;
    activeRunRef.current = null;
    lastEventIdRef.current = null;
    const id = next ?? newId("thread");
    setThreadId(id);
    saveSession(storageKey, { threadId: id });
  }, [storageKey]);

  const loadThread = useCallback(async (id: string) => {
    stoppingRef.current = true;
    abortRef.current?.abort();
    setThreadId(id);
    setFrames([]);
    setError(null);
    saveSession(storageKey, { threadId: id });
    try {
      setState(await readThread(id, request));
    } catch (err) {
      setError(`Could not load the thread: ${(err as Error).message}`);
    }
  }, [request, storageKey]);

  const clearFrames = useCallback(() => setFrames([]), []);

  // What the server offers on return, asked once rather than guessed. The
  // buttons and banners that depend on it are wrong until this answers, so it
  // is read from health rather than waiting for the first run's headers.
  useEffect(() => {
    void request(`/health`)
      .then((response) => response.json())
      .then((data) => setResumeMode(normalizeResume(data.resumeMode)))
      .catch(() => setResumeMode("none"));
  }, [request]);

  // A reload lands here. Wait until health has said what resume can do —
  // treating the initial `null` as `"none"` would skip attach forever.
  const restored = useRef(false);
  useEffect(() => {
    if (resumeMode === null || restored.current || !loadSession(storageKey)) return;
    restored.current = true;
    const session = loadSession(storageKey)!;

    void (async () => {
      const history = await readThread(session.threadId, request).catch(() => null);
      if (history) setState(history);
      if (resumeMode === "none") return;

      const active = (await request(`/threads/${session.threadId}/active`)
        .then((response) => (response.ok ? response.json() : []))
        .catch(() => [])) as Array<{ runId: string; status?: string; input?: string }>;
      const live = active.find((record) => record.status === "running");
      // Paused = waiting on a form. Frames already restored pendingTools;
      // tailing a paused run would hang with isStreaming=true and lock Submit.
      if (!live && active.some((record) => record.status === "paused")) return;

      const runId = live?.runId ?? session.runId;
      if (!runId) return;

      if (live?.input) {
        setState((prev) => {
          if (prev.messages.some((m) => m.role === "user" && m.content === live.input)) {
            return prev;
          }
          const userMsg: ChatMessage = {
            ...newAssistantMessage(`user-${runId}`),
            role: "user",
            content: live.input!,
            texts: { "text-0": live.input! },
            order: [{ kind: "text", key: "text-0" }],
          };
          return { ...prev, messages: [...prev.messages, userMsg] };
        });
      }

      // History already reduced what this client had; `after` is that cursor.
      // An empty transcript (hot log not yet readable) replays from the start.
      const after =
        history && history.messages.length > 0 ? session.lastEventId : undefined;
      const clock = new RunClock(session.threadId);
      activeRunRef.current = runId;
      lastEventIdRef.current = after ?? null;
      // Attach has no RUN_STARTED; patchCurrent would otherwise drop tokens.
      setState((prev) => ({ ...prev, currentId: `assistant-${runId}` }));
      setStreaming(true);
      setStats({ ...EMPTY_STATS, startedAt: clock.startedAt });
      const sink = makeSink(clock);
      const onKeepalive = () => {
        clock.notePing();
        setStats(clock.summary());
      };
      try {
        await getSse(
          `${apiBase}/runs/${runId}/attach${after ? `?after=${encodeURIComponent(after)}` : ""}`,
          {
            headers: headersRef.current(),
            onFrame: (frame) => deliver(frame, sink, setError),
            onKeepalive,
          },
        );
      } catch (err) {
        const message = (err as Error).message ?? "";
        // Idle drop or half-open socket: re-attach from the last reduced frame.
        if ((err as Error).name === "AbortError" && !stoppingRef.current) {
          const recovered = await reconnect(runId, sink, setError, setReconnecting, onKeepalive);
          if (recovered) return;
        }
        // A leftover runId after the process restarted is not worth an error
        // banner — the transcript is already on screen from history.
        if (!message.includes("HTTP 404")) {
          setError("Could not follow the run that was still going.");
        }
      } finally {
        setStreaming(false);
        setReconnecting(false);
        activeRunRef.current = null;
        setStats(clock.summary());
        saveSession(storageKey, { threadId: session.threadId });
        setState(dropEmptyTail);
      }
    })();
  }, [apiBase, makeSink, request, resumeMode, storageKey]);

  return useMemo(
    () => ({
      messages: state.messages,
      frames,
      violations: state.violations,
      sharedState: state.sharedState,
      statePatches: state.statePatches,
      stats,
      pendingTools: state.pendingTools,
      lastRequest,
      isStreaming,
      isReconnecting,
      resumeMode: resumeMode ?? "none",
      error,
      threadId,
      send,
      respondToTools,
      stop,
      reset,
      loadThread,
      clearFrames,
      registerFrontendTool,
    }),
    [
      state,
      frames,
      stats,
      lastRequest,
      isStreaming,
      isReconnecting,
      resumeMode,
      error,
      threadId,
      send,
      respondToTools,
      stop,
      reset,
      loadThread,
      clearFrames,
      registerFrontendTool,
    ],
  );
}

// ── reading a thread back ─────────────────────────────────────────────────

/**
 * Rebuild a thread, preferring stored frames over rebuilt messages.
 *
 * Frames are what the user watched — from the history archive once the run
 * has settled, or from Redis while it still remembers. `/messages` rebuilds a
 * turn from what Agno kept for the model, which is a lossy fallback for when
 * both of those are empty.
 */
async function readThread(threadId: string, request: FetchFn): Promise<ChatState> {
  const response = await request(`/threads/${threadId}/frames`);
  if (response.ok) {
    const { frames } = (await response.json()) as {
      frames: Array<{ id: string; event: AguiEvent }>;
    };
    if (frames.length > 0) {
      const session = await readSessionMessages(threadId, request);
      const replayed = replayFrames(frames);
      const hasUser = replayed.messages.some((message) => message.role === "user");
      const baseState = hasUser
        ? replayed
        : interleave(replayed, session.filter((message) => message.role === "user"));
      return resolvePendingAgainstActive(
        threadId,
        request,
        mergeSessionToolResults(
          baseState,
          session,
          confirmationIdsFromFrames(frames),
        ),
      );
    }
  } else if (response.status !== 501) {
    throw new Error(`HTTP ${response.status}`);
  }

  const fallback = await request(`/threads/${threadId}/messages`);
  if (!fallback.ok) throw new Error(`HTTP ${fallback.status}`);
  const raw = (await fallback.json()) as Array<Record<string, unknown>>;
  return { ...EMPTY_STATE, messages: raw.map(fromReplayMessage) };
}

/**
 * The user's turns, which frames do not contain.
 *
 * A run's frames start at `RUN_STARTED`: the prompt that caused it was in the
 * request body and was never an event. Agno's session is the only record of it,
 * so the two sources are stitched together — assistant turns from frames, in
 * their original streamed shape, and the questions from the session.
 */
async function readSessionMessages(threadId: string, request: FetchFn): Promise<ChatMessage[]> {
  const response = await request(`/threads/${threadId}/messages`);
  if (!response.ok) return [];
  const raw = (await response.json()) as Array<Record<string, unknown>>;
  return raw.map(fromReplayMessage);
}

async function resolvePendingAgainstActive(
  threadId: string,
  request: FetchFn,
  state: ChatState,
): Promise<ChatState> {
  if (state.pendingTools.length === 0) return state;
  const active = (await request(`/threads/${threadId}/active`)
    .then((response) => (response.ok ? response.json() : []))
    .catch(() => [])) as Array<{ status?: string }>;
  return active.some((record) => record.status === "paused") ? state : settleAnsweredPause(state);
}

/** Frame numbering and timings for the inspector. */
class RunClock {
  readonly startedAt = performance.now();
  private sequence = 0;
  private lastOffsetMs = 0;
  private firstToken: number | null = null;
  private counts: Record<string, number> = {};
  private pingCount = 0;
  private lastPingAt: number | null = null;
  private lastPingOffsetMs: number | null = null;
  private lastPingTime: string | null = null;
  private lastFrameAt: number | null = null;

  constructor(readonly threadId: string) {}

  tick(event: AguiEvent): DebugFrame {
    const type = String(event.type);
    this.sequence += 1;
    this.counts[type] = (this.counts[type] ?? 0) + 1;
    const now = performance.now();
    this.lastFrameAt = now;
    const offsetMs = now - this.startedAt;
    const deltaMs = offsetMs - this.lastOffsetMs;
    this.lastOffsetMs = offsetMs;
    if (this.firstToken === null && type === "TEXT_MESSAGE_CONTENT") this.firstToken = offsetMs;
    return { sequence: this.sequence, offsetMs, deltaMs, type, event };
  }

  /** Wire `: ping` — not an AG-UI frame; Events tab stays clean. */
  notePing(): void {
    const now = performance.now();
    this.pingCount += 1;
    this.lastPingAt = now;
    this.lastPingOffsetMs = now - this.startedAt;
    this.lastPingTime = new Date().toTimeString().slice(0, 8);
  }

  summary(): RunStats {
    return {
      startedAt: this.startedAt,
      timeToFirstTokenMs: this.firstToken,
      totalMs: performance.now() - this.startedAt,
      frameCount: this.sequence,
      countsByType: this.counts,
      pingCount: this.pingCount,
      lastPingAt: this.lastPingAt,
      lastPingOffsetMs: this.lastPingOffsetMs,
      lastPingTime: this.lastPingTime,
      lastFrameAt: this.lastFrameAt,
    };
  }
}

function deliver(
  frame: { data: string; id?: string },
  sink: (event: AguiEvent, id?: string) => void,
  fail: (message: string) => void,
) {
  try {
    sink(JSON.parse(frame.data) as AguiEvent, frame.id);
  } catch {
    // A frame we cannot parse is worth surfacing, not swallowing.
    fail(`Could not parse an SSE frame: ${frame.data.slice(0, 200)}`);
  }
}

function readResumeMode(response: Response): ResumeMode {
  return normalizeResume(response.headers.get(RESUME_HEADER));
}

function normalizeResume(value: unknown): ResumeMode {
  return value === "live" || value === "history" ? value : "none";
}

function loadSession(storageKey: string): StoredSession | null {
  try {
    const raw = localStorage.getItem(storageKey);
    return raw ? (JSON.parse(raw) as StoredSession) : null;
  } catch {
    return null;
  }
}

function saveSession(storageKey: string, session: StoredSession): void {
  try {
    localStorage.setItem(storageKey, JSON.stringify(session));
  } catch {
    // Private browsing, a full quota — losing the ability to resume is not a
    // reason to lose the run.
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
