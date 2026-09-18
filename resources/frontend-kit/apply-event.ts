/**
 * Pure AG-UI reducer — the sequencer mirror.
 *
 * Live SSE, `/frames` replay, and `/attach` resume all go through `applyEvent`.
 * That is the whole of refresh-and-resume: one function, three sources.
 */

import {
  newAssistantMessage,
  newSubAgentRun,
  type AguiEvent,
  type ChatMessage,
  type MessageBody,
  type PendingTool,
  type StatePatchOp,
  type StreamUIBlock,
  type SubAgentRun,
  type ToolCall,
  type Violation,
} from "./agui";

export interface ChatState {
  messages: ChatMessage[];
  /** The assistant message the current run is writing into. */
  currentId: string | null;
  sharedState: unknown;
  statePatches: StatePatchOp[];
  pendingTools: PendingTool[];
  violations: Violation[];
}

export const EMPTY_STATE: ChatState = {
  messages: [],
  currentId: null,
  sharedState: null,
  statePatches: [],
  pendingTools: [],
  violations: [],
};

export function confirmationIdsFromFrames(
  frames: Array<{ event: AguiEvent }>,
): Set<string> {
  const ids = new Set<string>();
  for (const frame of frames) {
    if (frame.event.type !== "CUSTOM" || frame.event.name !== "run.paused") continue;
    const value = frame.event.value as { pauseType?: string; toolCallId?: string } | undefined;
    if (value?.pauseType === "confirmation" && value.toolCallId) ids.add(value.toolCallId);
  }
  return ids;
}

/**
 * Resume frames often omit TOOL_CALL_RESULT. Session still has the payload
 * (frontend tools) or at least status (confirmation deny = error + empty).
 */
export function mergeSessionToolResults(
  state: ChatState,
  session: ChatMessage[],
  confirmationIds: Set<string>,
): ChatState {
  const byId = new Map<string, ToolCall>();
  for (const message of session) {
    for (const call of message.toolCalls) {
      if (call.id) byId.set(call.id, call);
    }
  }
  if (byId.size === 0) return state;
  let next = state;
  for (const [id, sessionCall] of byId) {
    const patched = patchToolCallAnywhere(next, id, (call) => {
      let updated = call;
      if (!call.result && sessionCall.result) {
        updated = applyToolEvent(call, "TOOL_CALL_RESULT", {
          type: "TOOL_CALL_RESULT",
          content: sessionCall.result,
        });
      }
      if (confirmationIds.has(id) && !hasAccepted(updated.args)) {
        const denied = sessionCall.status === "error" && !sessionCall.result;
        updated = {
          ...updated,
          args: argsFromToolAnswer(updated.args, JSON.stringify({ accepted: !denied })),
          status: denied ? "error" : updated.status,
        };
      }
      return updated;
    });
    next = patched ?? next;
  }
  return next;
}

function hasAccepted(args: string): boolean {
  try {
    const parsed = JSON.parse(args || "{}") as unknown;
    return Boolean(parsed && typeof parsed === "object" && "accepted" in parsed);
  } catch {
    return false;
  }
}

export function replayFrames(frames: Array<{ id: string; event: AguiEvent }>): ChatState {
  let state = EMPTY_STATE;
  for (const frame of frames) state = applyEvent(state, frame.event, 0);
  return { ...dropEmptyTail(state), currentId: null };
}

/** A later run means the user already answered (or abandoned) the pause. */
export function settleAnsweredPause(state: ChatState): ChatState {
  if (state.pendingTools.length === 0) return state;
  return {
    ...state,
    pendingTools: [],
    messages: state.messages.map((message) => ({
      ...message,
      toolCalls: message.toolCalls.map((call) =>
        call.status === "waiting" ? { ...call, status: "done" as const } : call,
      ),
    })),
  };
}

/**
 * Question, answer, question, answer — runs in order, prompts in order.
 *
 * HITL resume writes the same user turn into Agno's session again. Consecutive
 * copies of that prompt are one turn, not two.
 */
export function interleave(replayed: ChatState, users: ChatMessage[]): ChatState {
  const prompts = dedupeConsecutiveUsers(users);
  const answers = replayed.messages;
  const messages: ChatMessage[] = [];
  for (let index = 0; index < Math.max(prompts.length, answers.length); index += 1) {
    if (prompts[index]) messages.push(prompts[index]);
    if (answers[index]) messages.push(answers[index]);
  }
  return { ...replayed, messages };
}

function dedupeConsecutiveUsers(users: ChatMessage[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (const user of users) {
    const prev = out.at(-1);
    if (prev && prev.content === user.content) continue;
    out.push(user);
  }
  return out;
}

// ── the reducer ───────────────────────────────────────────────────────────

export function applyEvent(state: ChatState, event: AguiEvent, offsetMs: number): ChatState {
  const type = String(event.type);

  switch (type) {
    case "RUN_STARTED": {
      // Replaying a run from its start must not append to what a previous pass
      // already built, so the message is created fresh here and keyed by run.
      // A new run also settles any HITL form: the user already answered (or
      // the pause was abandoned). Historical `run.paused` frames must not
      // keep showing the form after resume.
      const settled = settleAnsweredPause(state);
      const runId = String(event.runId ?? newId("run"));
      const id = `assistant-${runId}`;
      const raw = (event.rawEvent ?? event.raw_event) as Record<string, unknown> | undefined;
      const userInput = (raw?.user_input ?? raw?.input ?? (event as Record<string, unknown>).user_input) as string | undefined;

      let messages = [...settled.messages.filter((m) => m.id !== id)];
      if (userInput && !messages.some((m) => m.role === "user" && m.content === userInput)) {
        const userMsg: ChatMessage = {
          ...newAssistantMessage(`user-${runId}`),
          role: "user",
          content: userInput,
          texts: { "text-0": userInput },
          order: [{ kind: "text", key: "text-0" }],
        };
        messages.push(userMsg);
      }
      messages.push(newAssistantMessage(id));

      return {
        ...settled,
        currentId: id,
        messages,
      };
    }
    case "STATE_SNAPSHOT":
      return { ...state, sharedState: event.snapshot };
    case "STATE_DELTA": {
      const ops = (event.delta as JsonPatchOp[]) ?? [];
      return {
        ...state,
        statePatches: [...state.statePatches, ...ops.map((op) => ({ ...op, offsetMs }))],
        sharedState: applyJsonPatch(state.sharedState, ops),
      };
    }
    case "CUSTOM":
      return applyCustom(state, event);
    case "TOOL_CALL_START":
    case "TOOL_CALL_ARGS":
    case "TOOL_CALL_END":
    case "TOOL_CALL_RESULT": {
      const id = String(event.toolCallId ?? "");
      const existing = id ? patchToolCallAnywhere(state, id, (call) => applyToolEvent(call, type, event)) : null;
      let next = existing ?? patchCurrent(state, (message) => reduceMessage(message, type, event));
      // `run.paused` is emitted before the tool-call frames. The card is
      // created as running; flip it to waiting once we know this id is a form.
      if (
        type === "TOOL_CALL_START" &&
        next.pendingTools.some((tool) => tool.toolCallId === id)
      ) {
        next =
          patchToolCallAnywhere(next, id, (call) => ({
            ...call,
            status: "waiting" as const,
            endedAt: call.endedAt ?? Date.now(),
          })) ?? next;
      }
      return type === "TOOL_CALL_RESULT"
        ? { ...next, pendingTools: next.pendingTools.filter((tool) => tool.toolCallId !== id) }
        : next;
    }
    case "RUN_ERROR":
      return patchCurrent(state, (message) => ({
        ...message,
        error: String(event.message ?? "The run failed."),
      }));
    default:
      return patchCurrent(state, (message) => reduceMessage(message, type, event));
  }
}

function patchCurrent(state: ChatState, patch: (message: ChatMessage) => ChatMessage): ChatState {
  if (!state.currentId) return state;
  return {
    ...state,
    messages: state.messages.map((message) =>
      message.id === state.currentId ? patch(message) : message,
    ),
  };
}

function reduceMessage(message: ChatMessage, type: string, event: AguiEvent): ChatMessage {
  if (type === "STEP_STARTED") {
    return { ...message, steps: [...message.steps, String(event.stepName ?? "")] };
  }
  if (type === "STEP_FINISHED") {
    return { ...message, steps: message.steps.filter((step) => step !== event.stepName) };
  }

  // While a sub-agent is streaming, everything belongs to it rather than to the
  // parent — including the prose, which would otherwise become part of the
  // parent's answer and be sent back as history.
  const body = reduceInActiveBody(message, (target) => reduceBody(target, type, event));
  if (type !== "TEXT_MESSAGE_CONTENT" || openSubAgent(message)) return body;
  return { ...body, content: message.content + String(event.delta ?? "") };
}

/** The one sub-agent currently streaming, if any. */
function openSubAgent(message: ChatMessage): SubAgentRun | undefined {
  return message.subAgents.find((run) => run.status === "running");
}

/**
 * Apply `patch` to whichever body owns the frames arriving right now.
 *
 * This is the whole of the nesting: one place decides the target, and every
 * reducer below stays unaware that sub-agents exist.
 */
function reduceInActiveBody(
  message: ChatMessage,
  patch: <T extends MessageBody>(body: T) => T,
): ChatMessage {
  const open = openSubAgent(message);
  if (!open) return patch(message);
  return {
    ...message,
    subAgents: message.subAgents.map((run) => (run.subRunId === open.subRunId ? patch(run) : run)),
  };
}

function reduceBody<T extends MessageBody>(body: T, type: string, event: AguiEvent): T {
  switch (type) {
    case "TEXT_MESSAGE_CONTENT": {
      const delta = String(event.delta ?? "");
      if (!delta) return body;

      // Text that resumes after a card or a tool call opens a new run, so it
      // renders below that element. Appending to the run above instead would
      // silently move the model's prose above its own cards.
      const last = body.order.at(-1);
      const continuing = last?.kind === "text";
      const key = continuing ? last.key : `text-${body.order.length}`;

      return {
        ...body,
        texts: { ...body.texts, [key]: (body.texts[key] ?? "") + delta },
        order: continuing ? body.order : [...body.order, { kind: "text" as const, key }],
      };
    }
    case "REASONING_MESSAGE_CONTENT": {
      const delta = String(event.delta ?? "");
      if (!delta) return body;
      // One collapsible per reasoning session, placed where that session began.
      // The runtime closes reasoning at a tool call or the answer and gives the
      // next session its own id, so a model that thinks around a tool call
      // gives two blocks in the right places.
      const key = String(event.messageId ?? "reasoning");
      const seen = key in body.reasonings;
      return {
        ...body,
        reasonings: { ...body.reasonings, [key]: (body.reasonings[key] ?? "") + delta },
        order: seen ? body.order : [...body.order, { kind: "reasoning" as const, key }],
      };
    }
    case "TOOL_CALL_START": {
      const call: ToolCall = {
        id: String(event.toolCallId),
        name: String(event.toolCallName ?? "tool"),
        args: "",
        result: "",
        status: "running",
        startedAt: Date.now(),
      };
      return {
        ...body,
        toolCalls: [...body.toolCalls, call],
        order: [...body.order, { kind: "tool", key: call.id }],
      };
    }
    case "TOOL_CALL_ARGS":
      return patchToolCall(body, String(event.toolCallId), (call) => ({
        ...call,
        args: mergeToolArgs(call.args, String(event.delta ?? "")),
      }));
    case "TOOL_CALL_END":
      return patchToolCall(body, String(event.toolCallId), (call) => ({
        ...call,
        endedAt: Date.now(),
      }));
    case "TOOL_CALL_RESULT": {
      const content = String(event.content ?? "");
      return patchToolCall(body, String(event.toolCallId), (call) => ({
        ...call,
        result: content,
        status: /"?error"?\s*:/i.test(content) ? "error" : "done",
      }));
    }
    default:
      return body;
  }
}

function applyCustom(state: ChatState, event: AguiEvent): ChatState {
  const name = String(event.name ?? "");
  const value = (event.value ?? {}) as Record<string, unknown>;

  if (name === "run.paused") {
    const pending = value as unknown as PendingTool;
    return {
      ...state,
      pendingTools: [...state.pendingTools, pending],
      messages: state.messages.map((message) => ({
        ...message,
        toolCalls: message.toolCalls.map((call) =>
          call.id === pending.toolCallId
            ? { ...call, status: "waiting" as const, endedAt: call.endedAt ?? Date.now() }
            : call,
        ),
      })),
    };
  }
  if (name === "debug.summary") {
    return { ...state, violations: (value.violations as Violation[]) ?? [] };
  }
  if (name === "subagent.start" || name === "subagent.end") {
    return patchCurrent(state, (message) => applySubAgent(message, name, value));
  }
  if (name.startsWith("ui.")) {
    return patchCurrent(state, (message) =>
      reduceInActiveBody(message, (body) => applyStreamUI(body, name, value)),
    );
  }
  return patchCurrent(state, (message) =>
    reduceInActiveBody(message, (body) => ({
      ...body,
      customEvents: [...body.customEvents, { name, value: event.value }],
      order: [...body.order, { kind: "custom" as const, key: String(body.customEvents.length) }],
    })),
  );
}

function applySubAgent(
  message: ChatMessage,
  name: string,
  value: Record<string, unknown>,
): ChatMessage {
  const subRunId = String(value.subRunId ?? "");
  if (!subRunId) return message;

  if (name === "subagent.start") {
    const run = newSubAgentRun(subRunId, String(value.name ?? "agent"), {
      description: value.description ? String(value.description) : undefined,
      prompt: value.prompt ? String(value.prompt) : undefined,
      toolCallId: value.toolCallId ? String(value.toolCallId) : undefined,
    });
    // The panel takes a slot in `order`, so the delegation renders where it
    // happened rather than being appended after the parent's answer.
    return {
      ...message,
      subAgents: [...message.subAgents, run],
      order: [...message.order, { kind: "subagent" as const, key: subRunId }],
    };
  }

  return {
    ...message,
    subAgents: message.subAgents.map((run) =>
      run.subRunId === subRunId
        ? {
            ...run,
            status: "done" as const,
            endedAt: Date.now(),
            elapsedMs: Number(value.elapsedMs ?? 0) || undefined,
          }
        : run,
    ),
  };
}

/**
 * Blocks of which only the latest matters, keyed by schema name.
 *
 * A todo list is one card the tool rewrites, not a stream of cards: `todo_write`
 * sends the full plan on every call. Nothing in the protocol says so — a block
 * is immutable once sent — so this is a rendering decision the client makes
 * about a schema it knows the shape of.
 */
const REPLACING_SCHEMAS = new Set(["todo-list", "research-graph"]);

function applyStreamUI<T extends MessageBody>(
  body: T,
  name: string,
  value: Record<string, unknown>,
): T {
  const blockId = String(value.blockId ?? "");
  if (!blockId) return body;

  if (name === "ui.block.start") {
    const block: StreamUIBlock = {
      blockId,
      index: Number(value.index ?? 0),
      schema: value.schema ? String(value.schema) : undefined,
      props: (value.props as Record<string, unknown>) ?? {},
      body: value.body === "text" ? "text" : "items",
      itemSchema: value.itemSchema ? String(value.itemSchema) : undefined,
      items: [],
      text: "",
      complete: false,
      error: value.error as string | undefined,
      raw: value.raw as string | undefined,
    };
    const previous = REPLACING_SCHEMAS.has(block.schema ?? "")
      ? body.uiBlocks.findIndex((existing) => existing.schema === block.schema)
      : -1;
    if (previous >= 0) {
      // The tool sent the whole list again, so the earlier block is a stale copy
      // of the same card rather than a second card. Substituting it in place
      // keeps the plan where it first appeared instead of letting it walk down
      // the message as the model revises it. The protocol still never updates a
      // block — this is the client choosing which of two blocks to draw.
      const uiBlocks = [...body.uiBlocks];
      const stale = uiBlocks[previous].blockId;
      uiBlocks[previous] = block;
      return {
        ...body,
        uiBlocks,
        order: body.order.map((slot) =>
          slot.kind === "ui" && slot.key === stale ? { ...slot, key: blockId } : slot,
        ),
      };
    }

    return {
      ...body,
      uiBlocks: [...body.uiBlocks, block],
      order: [...body.order, { kind: "ui", key: blockId }],
    };
  }

  return {
    ...body,
    uiBlocks: body.uiBlocks.map((block) => {
      if (block.blockId !== blockId) return block;
      switch (name) {
        case "ui.item":
          return {
            ...block,
            items: [
              ...block.items,
              {
                index: Number(value.index ?? block.items.length),
                schema: (value.schema as string | undefined) ?? block.itemSchema,
                data: value.data,
                resolved: value.resolved as Record<string, unknown> | undefined,
                resolveError: value.resolveError as string | undefined,
                raw: value.raw as string | undefined,
                error: value.error as string | undefined,
              },
            ],
          };
        case "ui.text":
          // Appending is what makes a hundred-line code card readable while it
          // is still being written, instead of appearing all at once at the end.
          return { ...block, text: block.text + String(value.delta ?? "") };
        default:
          return {
            ...block,
            complete: true,
            truncated: Boolean(value.truncated),
            savedPath: value.savedPath ? String(value.savedPath) : block.savedPath,
            relativePath: value.relativePath ? String(value.relativePath) : block.relativePath,
            bytes: typeof value.bytes === "number" ? value.bytes : block.bytes,
            text: value.text !== undefined ? String(value.text) : block.text,
            persistenceError: value.persistenceError
              ? String(value.persistenceError)
              : block.persistenceError,
          };
      }
    }),
  };
}

/** Drop the assistant message if the run ended without producing anything. */
export function dropEmptyTail(state: ChatState): ChatState {
  return {
    ...state,
    messages: state.messages.filter(
      (message) => message.id !== state.currentId || !isEmptyMessage(message),
    ),
  };
}

/** `none` / Stop: no more frames are coming — do not leave tools spinning. */
export function interruptOpenWork(state: ChatState): ChatState {
  return {
    ...state,
    messages: state.messages.map((message) => ({
      ...message,
      toolCalls: message.toolCalls.map((call) =>
        call.status === "running"
          ? { ...call, status: "error" as const, endedAt: call.endedAt ?? Date.now() }
          : call,
      ),
      subAgents: message.subAgents.map((run) =>
        run.status === "running"
          ? { ...run, status: "done" as const, endedAt: run.endedAt ?? Date.now() }
          : run,
      ),
    })),
  };
}

function patchToolCall<T extends MessageBody>(
  body: T,
  id: string,
  patch: (call: ToolCall) => ToolCall,
): T {
  return {
    ...body,
    toolCalls: body.toolCalls.map((call) => (call.id === id ? patch(call) : call)),
  };
}

/** Resume repeats TOOL_CALL_* for the same id — write onto the original card. */
function patchToolCallAnywhere(
  state: ChatState,
  id: string,
  patch: (call: ToolCall) => ToolCall,
): ChatState | null {
  if (!state.messages.some((message) => messageHasTool(message, id))) return null;
  return {
    ...state,
    messages: state.messages.map((message) =>
      messageHasTool(message, id) ? mapToolCalls(message, id, patch) : message,
    ),
  };
}

function messageHasTool(message: ChatMessage, id: string): boolean {
  return (
    message.toolCalls.some((call) => call.id === id) ||
    message.subAgents.some((run) => run.toolCalls.some((call) => call.id === id))
  );
}

function mapToolCalls(message: ChatMessage, id: string, patch: (call: ToolCall) => ToolCall): ChatMessage {
  return {
    ...message,
    toolCalls: message.toolCalls.map((call) => (call.id === id ? patch(call) : call)),
    subAgents: message.subAgents.map((run) => ({
      ...run,
      toolCalls: run.toolCalls.map((call) => (call.id === id ? patch(call) : call)),
    })),
  };
}

function applyToolEvent(call: ToolCall, type: string, event: AguiEvent): ToolCall {
  if (type === "TOOL_CALL_START") return call;
  if (type === "TOOL_CALL_ARGS") {
    return { ...call, args: mergeToolArgs(call.args, String(event.delta ?? "")) };
  }
  if (type === "TOOL_CALL_END") {
    return { ...call, endedAt: call.endedAt ?? Date.now() };
  }
  const content = String(event.content ?? "");
  let args = call.args;
  if ((!args || args === "{}") && content) {
    try {
      const parsed = JSON.parse(content) as unknown;
      if (parsed && typeof parsed === "object") args = content;
    } catch {
      /* result is a plain string — leave args alone */
    }
  }
  return {
    ...call,
    args,
    result: content,
    status: /"?error"?\s*:/i.test(content) ? "error" : "done",
  };
}

function mergeToolArgs(current: string, delta: string): string {
  if (!delta) return current;
  if (!current || current === "{}") return delta;
  try {
    const cur = JSON.parse(current) as unknown;
    const next = JSON.parse(delta) as unknown;
    if (cur && typeof cur === "object" && next && typeof next === "object") {
      return JSON.stringify({ ...(cur as object), ...(next as object) });
    }
    return delta;
  } catch {
    return current + delta;
  }
}

/** Show the HITL answer on the card as soon as the user submits it. */
export function stampToolAnswers(
  state: ChatState,
  results: Array<{ toolCallId: string; content: string }>,
): ChatState {
  return results.reduce((next, result) => {
    const patched = patchToolCallAnywhere(next, result.toolCallId, (call) => ({
      ...call,
      args: argsFromToolAnswer(call.args, result.content),
      status: "done" as const,
    }));
    return patched ?? next;
  }, state);
}

function argsFromToolAnswer(current: string, content: string): string {
  try {
    const parsed = JSON.parse(content) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object") return current;
    if (parsed.values && typeof parsed.values === "object") {
      return JSON.stringify(parsed.values);
    }
    if (parsed.selections && typeof parsed.selections === "object") {
      return JSON.stringify(parsed.selections);
    }
    if ("accepted" in parsed) {
      const base = current ? (JSON.parse(current) as Record<string, unknown>) : {};
      return JSON.stringify({ ...base, accepted: parsed.accepted });
    }
    return JSON.stringify(parsed);
  } catch {
    return current || content;
  }
}

function isEmptyMessage(message: ChatMessage): boolean {
  return (
    !message.content &&
    Object.keys(message.reasonings).length === 0 &&
    !message.error &&
    message.toolCalls.length === 0 &&
    message.uiBlocks.length === 0 &&
    message.customEvents.length === 0 &&
    message.subAgents.length === 0
  );
}

export function toWireMessage(message: ChatMessage): Record<string, unknown> {
  return { id: message.id, role: message.role, content: message.content };
}

/**
 * Rebuild one turn from Agno's session, for servers with no frame log.
 *
 * A stored turn is a handful of blobs rather than a stream, so the original
 * interleaving is gone and this can only approximate it. That approximation is
 * exactly what the frames route exists to avoid.
 */
export function fromReplayMessage(raw: Record<string, unknown>): ChatMessage {
  const toolCalls = ((raw.toolCalls as Array<Record<string, unknown>>) ?? []).map((call) => ({
    id: String(call.id ?? ""),
    name: String(call.name ?? "tool"),
    args: String(call.args ?? ""),
    result: String(call.result ?? ""),
    status: (call.status === "error" ? "error" : "done") as ToolCall["status"],
    startedAt: 0,
  }));
  const uiBlocks = ((raw.uiBlocks as Array<Record<string, unknown>>) ?? []).map((block) => ({
    blockId: String(block.blockId ?? ""),
    index: Number(block.index ?? 0),
    schema: block.schema ? String(block.schema) : undefined,
    props: (block.props as Record<string, unknown>) ?? {},
    body: (block.body === "text" ? "text" : "items") as StreamUIBlock["body"],
    itemSchema: block.itemSchema ? String(block.itemSchema) : undefined,
    items: (block.items as StreamUIBlock["items"]) ?? [],
    text: String(block.text ?? ""),
    complete: true,
    truncated: Boolean(block.truncated),
  }));
  const customEvents = ((raw.customEvents as Array<Record<string, unknown>>) ?? []).map(
    (entry) => ({ name: String(entry.name ?? ""), value: entry.value }),
  );
  const text = String(raw.content ?? "");
  const reasonings = Object.fromEntries(
    replayReasonings(raw.reasoning).map((session, index) => [`reasoning-${index}`, session]),
  );
  return {
    texts: { "text-0": text },
    reasonings,
    toolCalls,
    uiBlocks,
    customEvents,
    order: [
      ...customEvents.map((_, index) => ({ kind: "custom" as const, key: String(index) })),
      ...Object.keys(reasonings).map((key) => ({ kind: "reasoning" as const, key })),
      ...toolCalls.map((call) => ({ kind: "tool" as const, key: call.id })),
      { kind: "text" as const, key: "text-0" },
      ...uiBlocks.map((block) => ({ kind: "ui" as const, key: block.blockId })),
    ],
    id: String(raw.id ?? newId("msg")),
    role: raw.role === "user" ? "user" : "assistant",
    content: text,
    subAgents: [],
    steps: [],
  };
}

/** Agno stores reasoning as one string; older records used a list. */
function replayReasonings(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.map(String).filter(Boolean);
  return raw ? [String(raw)] : [];
}

interface JsonPatchOp {
  op: string;
  path: string;
  value?: unknown;
}

/** Enough of RFC 6902 for the operations Agno's differ produces. */
function applyJsonPatch(document: unknown, ops: JsonPatchOp[]): unknown {
  let result: unknown = structuredClone(document ?? {});
  for (const op of ops ?? []) {
    result = applyOne(result, op);
  }
  return result;
}

function applyOne(document: unknown, op: JsonPatchOp): unknown {
  const segments = op.path.split("/").slice(1).map(unescapePointer);
  if (segments.length === 0) return op.op === "remove" ? null : op.value;

  const root = document as Record<string, unknown>;
  let parent: any = root;
  for (const segment of segments.slice(0, -1)) {
    if (parent == null) return root;
    parent = parent[segment];
  }
  if (parent == null) return root;

  const last = segments[segments.length - 1];
  if (Array.isArray(parent)) {
    const index = last === "-" ? parent.length : Number(last);
    if (op.op === "remove") parent.splice(index, 1);
    else if (op.op === "add") parent.splice(index, 0, op.value);
    else parent[index] = op.value;
  } else if (op.op === "remove") {
    delete parent[last];
  } else {
    parent[last] = op.value;
  }
  return root;
}

function unescapePointer(segment: string): string {
  return segment.replace(/~1/g, "/").replace(/~0/g, "~");
}

export function newId(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 12)}`;
}
