/**
 * Live transcript over SSE.
 *
 * The API streams canonical events as `event: <type>\ndata: <json>\n\n` with an
 * `id: <seq>` field; the native EventSource echoes the last id as
 * `Last-Event-ID` on reconnect so the server resumes above the watermark
 * (src/nightdesk/api/routes/transcript.py). This module owns the connection,
 * the bounded event buffer, and the light dispatch helpers the renderer uses.
 *
 * The Python server-side renderer (transcript_view.py) is the source of truth
 * for event shapes; this is a pragmatic subset for readable live display, not a
 * byte-for-byte mirror.
 */
import { useEffect, useRef, useState } from "react";

export interface TranscriptEvent {
  /** canonical event type: assistant_text, thinking, tool_use, tool_result,
   *  result, meta, system, stats, subagent, rate_limit, worker_error, … */
  type: string;
  seq?: number;
  /** client-assigned stable key for React lists (seq when present). */
  _key: string;
  [k: string]: unknown;
}

/** Event types the API emits as named SSE events (see _format_sse). Legacy
 *  raw lines arrive as the default `message` event and are wrapped as `raw`. */
const KNOWN_EVENT_TYPES = [
  "assistant_text",
  "thinking",
  "tool_use",
  "tool_result",
  "result",
  "meta",
  "system",
  "stats",
  "subagent",
  "rate_limit",
  "worker_error",
  "user_message",
  "text",
  "user",
  "assistant",
  "cancelled",
  "preset",
  "steer_delivered",
  // Resident-agents events. Breadcrumb events (persisted, rendered by the
  // transcript view): needs_input, runtime_restarted, session_booting,
  // session_crashed. Control events (streamed, NOT persisted; the renderer
  // ignores them and the agent screen consumes them): pending_input,
  // pending_resolved, turn_complete, server_info.
  "needs_input",
  "runtime_restarted",
  "session_booting",
  "session_crashed",
  "pending_input",
  "pending_resolved",
  "turn_complete",
  "server_info",
] as const;

const MAX_EVENTS = 4000;

export type TranscriptStatus = "connecting" | "open" | "closed" | "error";

export interface LiveTranscript {
  events: TranscriptEvent[];
  status: TranscriptStatus;
}

/** Subscribe to a transcript SSE `path` while `enabled`. Both the per-ticket
 *  (`/api/v1/tickets/{id}/transcript`) and per-run
 *  (`/api/v1/runs/{rid}/transcript`) endpoints speak the same protocol. Returns
 *  the accumulated (bounded) event buffer and the connection status. */
export function useLiveTranscript(
  path: string | undefined,
  enabled: boolean,
): LiveTranscript {
  const [events, setEvents] = useState<TranscriptEvent[]>([]);
  const [status, setStatus] = useState<TranscriptStatus>("closed");
  const counter = useRef(0);

  useEffect(() => {
    if (!path || !enabled) {
      setStatus("closed");
      return;
    }
    setEvents([]);
    setStatus("connecting");
    counter.current = 0;

    const es = new EventSource(path, { withCredentials: true });

    const push = (type: string, raw: string) => {
      let parsed: Record<string, unknown> = {};
      try {
        const j = JSON.parse(raw);
        if (j && typeof j === "object") parsed = j as Record<string, unknown>;
        else parsed = { text: String(j) };
      } catch {
        parsed = { text: raw };
      }
      // seq may arrive as a number or a numeric string depending on transcript age.
      const seqRaw = parsed.seq;
      const seqNum =
        typeof seqRaw === "number"
          ? seqRaw
          : typeof seqRaw === "string" && seqRaw !== "" && !Number.isNaN(Number(seqRaw))
            ? Number(seqRaw)
            : undefined;
      const seq = seqNum;
      const evt: TranscriptEvent = {
        ...parsed,
        type: typeof parsed.type === "string" ? (parsed.type as string) : type,
        seq,
        _key: seq !== undefined ? `s${seq}` : `c${counter.current++}`,
      };
      setEvents((prev) => {
        const next = prev.length >= MAX_EVENTS ? prev.slice(prev.length - MAX_EVENTS + 1) : prev;
        return [...next, evt];
      });
    };

    es.onopen = () => setStatus("open");
    es.onerror = () => {
      // EventSource auto-reconnects; surface a soft error but keep the buffer.
      setStatus((s) => (s === "open" ? "connecting" : "error"));
    };
    es.onmessage = (e) => push("raw", e.data);
    for (const t of KNOWN_EVENT_TYPES) {
      es.addEventListener(t, (e) => push(t, (e as MessageEvent).data));
    }
    es.addEventListener("end", () => {
      setStatus("closed");
      es.close();
    });

    return () => es.close();
  }, [path, enabled]);

  return { events, status };
}

// --- Turn liveness ---------------------------------------------------------------

/** Events that (re)open a turn: the user spoke, steered, or resolved a pending
 *  input, so the agent is now working on a reply. Ticket runs have none of
 *  these — the whole run is one implicit turn. */
const TURN_START_TYPES = new Set(["user_message", "user", "steer_delivered", "pending_resolved"]);

/** Events that settle a turn. `result` is the persisted per-turn closer on both
 *  agent sessions and ticket runs; `turn_complete` is the streamed control
 *  sibling on agents. `needs_input` pauses the agent on a question/permission
 *  (not working), and errors/cancellation end the turn outright. */
const TURN_END_TYPES = new Set([
  "result",
  "turn_complete",
  "worker_error",
  "cancelled",
  "needs_input",
  "session_crashed",
]);

/**
 * True while a turn is in flight given the event buffer: no settling event has
 * landed since the last turn-opening event (or since the start of the stream,
 * which covers ticket runs — running from the first event until their single
 * trailing `result`). Callers AND this with the live `running` flag; a resident
 * agent idles "alive" between turns, so liveness alone over-reports.
 */
export function isTurnInFlight(events: TranscriptEvent[]): boolean {
  let lastStart = -1;
  let lastEnd = -1;
  events.forEach((e, i) => {
    if (TURN_START_TYPES.has(e.type)) lastStart = i;
    if (TURN_END_TYPES.has(e.type)) lastEnd = i;
  });
  return lastEnd === -1 || lastEnd < lastStart;
}

// --- Display helpers -----------------------------------------------------------

export interface ToolSummary {
  tag: string;
  primary: string;
  meta: string;
}

function str(v: unknown): string {
  return v == null ? "" : String(v);
}

/** One-line summary for a tool_use event. Port of transcript_view.tool_summary
 *  covering the common tools; unknown tools fall back to the tool name. */
export function toolSummary(evt: Record<string, unknown>): ToolSummary {
  const tool = str(evt.tool).trim();
  const input = (evt.input as Record<string, unknown>) ?? {};
  const path = () => str(input.file_path || input.path || "?");
  switch (tool) {
    case "Read": {
      const offset = input.offset as number | undefined;
      const limit = input.limit as number | undefined;
      let meta = "";
      if (offset != null && limit != null) meta = `lines ${offset}-${Number(offset) + Number(limit)}`;
      else if (offset != null) meta = `from line ${offset}`;
      return { tag: "Read", primary: path(), meta };
    }
    case "Write": {
      const content = str(input.content);
      const lc = content ? content.split("\n").length : 0;
      return { tag: "Write", primary: path(), meta: lc ? `${lc} lines` : "" };
    }
    case "Edit":
      return { tag: "Edit", primary: path(), meta: "" };
    case "MultiEdit": {
      const n = Array.isArray(input.edits) ? input.edits.length : 0;
      return { tag: "MultiEdit", primary: path(), meta: `${n} edits` };
    }
    case "Bash": {
      let first = str(input.command).trim().split("\n")[0] ?? "";
      if (first.length > 80) first = `${first.slice(0, 79)}…`;
      return { tag: "Bash", primary: first, meta: "" };
    }
    case "Glob":
      return { tag: "Glob", primary: str(input.pattern), meta: input.path ? `in ${input.path}` : "" };
    case "Grep":
      return { tag: "Grep", primary: str(input.pattern), meta: input.path ? `in ${input.path}` : "" };
    default:
      return { tag: tool || "tool", primary: "", meta: "" };
  }
}

// --- Todo / task list ----------------------------------------------------------

export type TodoStatus = "pending" | "in_progress" | "completed" | "deleted";
export interface TodoItem {
  id: string;
  label: string;
  activeForm: string;
  status: TodoStatus;
}

/** Current todo state from the stream. The agent tracks tasks with either the
 *  Task* tool family (TaskCreate/TaskUpdate) or a single TodoWrite call; this
 *  normalizes whichever is present into the live list. Mirrors
 *  transcript_view.build_todo_list. */
export function buildTodoList(events: TranscriptEvent[]): TodoItem[] {
  const hasTask = events.some(
    (e) => e.type === "tool_use" && (e.tool === "TaskCreate" || e.tool === "TaskUpdate"),
  );
  return hasTask ? todosFromTasks(events) : todosFromTodoWrite(events);
}

function todosFromTasks(events: TranscriptEvent[]): TodoItem[] {
  const items: TodoItem[] = [];
  const byId = new Map<string, TodoItem>();
  for (const e of events) {
    if (e.type !== "tool_use") continue;
    const input = (e.input as Record<string, unknown>) ?? {};
    if (e.tool === "TaskCreate") {
      const item: TodoItem = {
        id: String(items.length + 1),
        label: str(input.subject).trim(),
        activeForm: str(input.activeForm).trim(),
        status: "pending",
      };
      items.push(item);
      byId.set(item.id, item);
    } else if (e.tool === "TaskUpdate") {
      const tid = str(input.taskId).trim();
      const status = str(input.status).trim() as TodoStatus;
      const item = byId.get(tid);
      if (item && status) item.status = status;
    }
  }
  return items.filter((i) => i.status !== "deleted");
}

function todosFromTodoWrite(events: TranscriptEvent[]): TodoItem[] {
  let last: TranscriptEvent | undefined;
  for (const e of events) {
    if (e.type === "tool_use" && e.tool === "TodoWrite") last = e;
  }
  if (!last) return [];
  const todos = ((last.input as Record<string, unknown>)?.todos as unknown[]) ?? [];
  return todos.map((td, i) => {
    const t = (td as Record<string, unknown>) ?? {};
    return {
      id: String(i + 1),
      label: str(t.content).trim(),
      activeForm: str(t.activeForm).trim(),
      status: (str(t.status).trim() || "pending") as TodoStatus,
    };
  });
}

// --- Sub-agents ----------------------------------------------------------------

export interface SubagentSummary {
  /** the Task tool_use_id — used to jump to its card in the transcript */
  id: string;
  label: string;
  detail: string;
  steps: number;
  done: boolean;
  failed: boolean;
}

const SUBAGENT_MERGE = [
  "subagent_type",
  "task_type",
  "description",
  "status",
  "summary",
  "phase",
  "last_tool_name",
] as const;

/** One row per sub-agent (Task tool) in the stream: type, nested step count,
 *  and terminal status. Mirrors transcript_view.subagent_index. */
export function buildSubagentList(events: TranscriptEvent[]): SubagentSummary[] {
  const byKey = new Map<string, TranscriptEvent>();
  const order: string[] = [];
  for (const e of events) {
    if (e.type !== "subagent") continue;
    const key = str(e.task_id) || str(e.tool_use_id);
    if (!key) continue;
    if (!byKey.has(key)) {
      byKey.set(key, { ...e });
      order.push(key);
    } else {
      const base = byKey.get(key)!;
      for (const k of SUBAGENT_MERGE) {
        const v = e[k];
        if (v !== undefined && v !== null && v !== "") base[k] = v;
      }
    }
  }
  const childCount = new Map<string, number>();
  for (const e of events) {
    const p = e.parent_tool_use_id;
    if (typeof p === "string" && e.type === "tool_use") {
      childCount.set(p, (childCount.get(p) ?? 0) + 1);
    }
  }
  return order.map((key) => {
    const ev = byKey.get(key)!;
    const tuid = str(ev.tool_use_id);
    const status = str(ev.status).toLowerCase();
    return {
      id: tuid || key,
      label: (str(ev.subagent_type) || str(ev.task_type).replace(/_/g, " ") || "subagent").trim(),
      detail: str(ev.description) || str(ev.last_tool_name),
      steps: childCount.get(tuid) ?? 0,
      done: str(ev.phase) === "notification" || status === "completed",
      failed: ["failed", "error", "errored"].includes(status),
    };
  });
}

export interface TailLine {
  kind: "text" | "tool" | "result" | "thinking" | "error" | "idle";
  text: string;
}

/** The latest human-meaningful line for a running ticket's card on the Desk. */
export function latestMeaningfulLine(events: TranscriptEvent[]): TailLine {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    switch (e.type) {
      case "assistant_text":
      case "text": {
        const t = str(e.text).trim().replace(/\s+/g, " ");
        if (t) return { kind: "text", text: t.slice(0, 160) };
        break;
      }
      case "tool_use": {
        const s = toolSummary(e);
        return { kind: "tool", text: `${s.tag}${s.primary ? ` · ${s.primary}` : ""}` };
      }
      case "thinking": {
        const t = str(e.text).trim().replace(/\s+/g, " ");
        if (t) return { kind: "thinking", text: t.slice(0, 140) };
        break;
      }
      case "worker_error":
        return { kind: "error", text: str(e.message || e.text || "worker error").slice(0, 160) };
      case "result": {
        const t = str(e.summary || e.text).trim().replace(/\s+/g, " ");
        if (t) return { kind: "result", text: t.slice(0, 160) };
        break;
      }
    }
  }
  return { kind: "idle", text: "Waiting for output…" };
}
