/**
 * AG-UI wire types and the client-side model they reduce into.
 *
 * Only the fields the kit reducer reads are declared. AG-UI events are open by
 * design, so unknown fields ride along on `AguiEvent` untouched — a debug
 * panel renders them from the raw payload rather than from a typed view.
 */

export type AguiEventType =
  | "RUN_STARTED"
  | "RUN_FINISHED"
  | "RUN_ERROR"
  | "STEP_STARTED"
  | "STEP_FINISHED"
  | "TEXT_MESSAGE_START"
  | "TEXT_MESSAGE_CONTENT"
  | "TEXT_MESSAGE_END"
  | "TOOL_CALL_START"
  | "TOOL_CALL_ARGS"
  | "TOOL_CALL_END"
  | "TOOL_CALL_RESULT"
  | "REASONING_START"
  | "REASONING_MESSAGE_START"
  | "REASONING_MESSAGE_CONTENT"
  | "REASONING_MESSAGE_END"
  | "REASONING_END"
  | "STATE_SNAPSHOT"
  | "STATE_DELTA"
  | "CUSTOM"
  | "RAW";

export interface AguiEvent {
  type: AguiEventType | string;
  [key: string]: unknown;
}

/** Broad families, used for badge colours and filtering. */
export type EventFamily =
  | "run"
  | "text"
  | "tool"
  | "reasoning"
  | "state"
  | "step"
  | "custom"
  | "other";

export function eventFamily(type: string): EventFamily {
  if (type.startsWith("RUN_")) return "run";
  if (type.startsWith("TEXT_MESSAGE")) return "text";
  if (type.startsWith("TOOL_CALL")) return "tool";
  if (type.startsWith("REASONING")) return "reasoning";
  if (type.startsWith("STATE_")) return "state";
  if (type.startsWith("STEP_")) return "step";
  if (type === "CUSTOM") return "custom";
  return "other";
}

export const FAMILY_BADGE: Record<EventFamily, string> = {
  run: "bg-sky-500/15 text-sky-400",
  text: "bg-emerald-500/15 text-emerald-400",
  tool: "bg-amber-500/15 text-amber-400",
  reasoning: "bg-violet-500/15 text-violet-400",
  state: "bg-cyan-500/15 text-cyan-400",
  step: "bg-fuchsia-500/15 text-fuchsia-400",
  custom: "bg-rose-500/15 text-rose-400",
  other: "bg-zinc-500/15 text-zinc-400",
};

// ── client-side model ─────────────────────────────────────────────────────

export type ToolCallStatus = "running" | "waiting" | "done" | "error";

export interface ToolCall {
  id: string;
  name: string;
  args: string;
  result: string;
  status: ToolCallStatus;
  startedAt: number;
  endedAt?: number;
}

/**
 * One item inside a StreamUI block.
 *
 * `data` is what the model chose; `resolved` is what the server looked up on
 * the strength of it. Keeping them apart is what lets a renderer show a film's
 * real rating and still know which id the model actually picked — and lets a
 * card degrade rather than vanish when the lookup fails, which is what
 * `resolveError` is for.
 */
export interface StreamUIItem {
  index: number;
  schema?: string;
  data?: unknown;
  resolved?: Record<string, unknown>;
  resolveError?: string;
  raw?: string;
  error?: string;
}

export interface StreamUIBlock {
  blockId: string;
  index: number;
  /** The card type, as registered in the server's catalog. */
  schema?: string;
  /** Block-level fields from the fence header — a title, a column list. */
  props: Record<string, unknown>;
  /** `items` streams JSON lines; `text` streams a raw body, code most often. */
  body: "items" | "text";
  /** The item type when the block is homogeneous and items may omit theirs. */
  itemSchema?: string;
  items: StreamUIItem[];
  text: string;
  complete: boolean;
  truncated?: boolean;
  /** Set when the fence header itself was wrong, with `raw` holding it. */
  error?: string;
  raw?: string;
  savedPath?: string;
  relativePath?: string;
  bytes?: number;
  persistenceError?: string;
}

export interface CustomRecord {
  name: string;
  value: unknown;
}

/** One tool the agent is waiting on the user for. */
export interface PendingTool {
  pauseType: "confirmation" | "user_input" | "user_feedback" | "external_execution";
  toolCallId: string;
  toolName: string;
  toolArgs: Record<string, unknown>;
  userInputSchema?: Array<{
    name: string;
    description?: string | null;
    fieldType?: string | null;
    value?: unknown;
    options?: string[] | null;
    multiSelect?: boolean;
  }> | null;
}

/**
 * Everything one agent produced, in the order it produced it.
 *
 * A body is not tied to the message: a sub-agent streaming into the same run
 * fills its own, which is what keeps its prose out of the parent's answer and
 * its tool cards out of the parent's list.
 */
export interface MessageBody {
  /**
   * Text split into runs, keyed by the slot in `order` that renders it.
   *
   * One body can contain several runs because a card or a tool call may
   * arrive in the middle of the prose. Keeping them separate is what lets a
   * block stay where the model put it instead of everything collapsing into
   * one paragraph with the cards pushed below.
   */
  texts: Record<string, string>;
  /**
   * Reasoning split into sessions, keyed by reasoning message id.
   *
   * A model that thinks, calls a tool, then thinks again produces two, and they
   * belong either side of the tool call rather than merged into one block.
   */
  reasonings: Record<string, string>;
  toolCalls: ToolCall[];
  uiBlocks: StreamUIBlock[];
  customEvents: CustomRecord[];
  /**
   * Every renderable part, in the order it was streamed.
   *
   * Nothing renders from a fixed position: a `CUSTOM` notice that arrived
   * before the first token belongs above the answer, not swept to the bottom
   * of the bubble, and a card belongs where the model wrote it.
   */
  order: Array<{ kind: OrderSlotKind; key: string }>;
}

/** One delegation, bracketed on the wire by `subagent.start` / `subagent.end`. */
export interface SubAgentRun extends MessageBody {
  subRunId: string;
  name: string;
  /** The delegating tool's one-line summary of the task, if it gave one. */
  description?: string;
  /** The full prompt the sub-agent was given. */
  prompt?: string;
  /**
   * The parent tool call that delegated, when the server could name it
   * unambiguously. The delegating card is usually hidden in favour of this
   * panel, so this is what ties the two together in the debug tabs.
   */
  toolCallId?: string;
  status: "running" | "done";
  startedAt: number;
  endedAt?: number;
  /**
   * How long it took. Replay has no clock to subtract, so the duration travels
   * with the persisted record rather than being derived from the timestamps.
   */
  elapsedMs?: number;
}

export interface ChatMessage extends MessageBody {
  id: string;
  role: "user" | "assistant";
  /** Every text run concatenated — what gets sent back on the wire. */
  content: string;
  subAgents: SubAgentRun[];
  steps: string[];
  error?: string;
}

export type OrderSlotKind = "text" | "ui" | "tool" | "custom" | "reasoning" | "subagent";

export interface DebugFrame {
  sequence: number;
  offsetMs: number;
  /** Time since the previous frame. A large gap is the stream waiting. */
  deltaMs: number;
  type: string;
  event: AguiEvent;
}

export interface Violation {
  rule: string;
  detail: string;
  eventType?: string | null;
  repair?: string;
  index?: number;
}

/** One JSON Patch operation, stamped with when it arrived. */
export interface StatePatchOp {
  op: string;
  path: string;
  value?: unknown;
  offsetMs: number;
}

export interface RunStats {
  startedAt: number;
  timeToFirstTokenMs: number | null;
  totalMs: number | null;
  frameCount: number;
  countsByType: Record<string, number>;
  /** SSE comment keepalives (`: ping`) — not AG-UI frames. */
  pingCount: number;
  lastPingAt: number | null;
  lastFrameAt: number | null;
  lastPingOffsetMs: number | null;
  lastPingTime: string | null;
}

export const EMPTY_STATS: RunStats = {
  startedAt: 0,
  timeToFirstTokenMs: null,
  totalMs: null,
  frameCount: 0,
  countsByType: {},
  pingCount: 0,
  lastPingAt: null,
  lastFrameAt: null,
  lastPingOffsetMs: null,
  lastPingTime: null,
};

export function emptyBody(): MessageBody {
  return { texts: {}, reasonings: {}, toolCalls: [], uiBlocks: [], customEvents: [], order: [] };
}

export function newAssistantMessage(id: string): ChatMessage {
  return {
    ...emptyBody(),
    id,
    role: "assistant",
    content: "",
    subAgents: [],
    steps: [],
  };
}

export function newSubAgentRun(
  subRunId: string,
  name: string,
  detail: { description?: string; prompt?: string; toolCallId?: string } = {},
): SubAgentRun {
  return {
    ...emptyBody(),
    subRunId,
    name,
    ...detail,
    status: "running",
    startedAt: Date.now(),
  };
}

/** How much a sub-agent produced, for the collapsed panel header. */
export function summarizeBody(body: MessageBody): string {
  const chars = Object.values(body.texts).reduce((total, text) => total + text.length, 0);
  const parts: string[] = [];
  if (body.toolCalls.length) {
    parts.push(`${body.toolCalls.length} tool${body.toolCalls.length === 1 ? "" : "s"}`);
  }
  const sessions = Object.keys(body.reasonings).length;
  if (sessions) parts.push(`${sessions} reasoning`);
  if (chars) parts.push(`${chars.toLocaleString()} chars`);
  return parts.join(" · ");
}
