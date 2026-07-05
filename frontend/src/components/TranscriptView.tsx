import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Circle,
  ListPlus,
  Loader2,
  Terminal,
  Users,
  Wrench,
} from "lucide-react";
import type { TranscriptEvent } from "@/lib/transcript";
import { toolSummary } from "@/lib/transcript";
import { cn } from "@/lib/cn";

// Tool tag → accent color class.
const TOOL_TONE: Record<string, string> = {
  Read: "text-queued",
  Write: "text-success",
  Edit: "text-lamp",
  MultiEdit: "text-lamp",
  Bash: "text-dawn",
  Grep: "text-review",
  Glob: "text-review",
};

interface Node {
  event: TranscriptEvent;
  result?: TranscriptEvent;
  children: Node[];
}

const SUBAGENT_MERGE_FIELDS = [
  "subagent_type",
  "task_type",
  "description",
  "prompt",
  "status",
  "summary",
  "usage",
  "phase",
  "last_tool_name",
] as const;

function mergeSubagent(base: TranscriptEvent, e: TranscriptEvent) {
  for (const k of SUBAGENT_MERGE_FIELDS) {
    const v = e[k];
    if (v !== undefined && v !== null && v !== "") base[k] = v;
  }
}

function str(v: unknown): string {
  return v == null ? "" : String(v);
}

/**
 * Build the render tree: pair tool_use↔tool_result, drop a trailing result that
 * echoes the last assistant_text, fold subagent lifecycle events into one card,
 * and nest each subagent's child tool calls (matched by parent_tool_use_id)
 * under it. Mirrors transcript_view.pair_tool_events + group_by_subagent.
 */
function buildTree(events: TranscriptEvent[]): Node[] {
  const resultById = new Map<string, TranscriptEvent>();
  for (const e of events) {
    if (e.type === "tool_result" && typeof e.tool_use_id === "string") {
      resultById.set(e.tool_use_id, e);
    }
  }
  const subagentTuids = new Set<string>();
  for (const e of events) {
    if (e.type === "subagent" && typeof e.tool_use_id === "string") subagentTuids.add(e.tool_use_id);
  }

  // Duplicate trailing result detection.
  let lastAssistant: string | null = null;
  let lastResultIdx = -1;
  events.forEach((e, i) => {
    if (e.type === "assistant_text" && str(e.text).trim()) lastAssistant = str(e.text);
    if (e.type === "result") lastResultIdx = i;
  });
  const dropResult =
    lastResultIdx >= 0 &&
    lastAssistant !== null &&
    str(events[lastResultIdx].summary).trim() === str(lastAssistant).trim() &&
    str(events[lastResultIdx].summary).trim() !== "";

  const paired = new Set<string>();
  const nodesByTuid = new Map<string, Node>();
  const subagentByKey = new Map<string, Node>();
  const out: Node[] = [];

  const makeToolNode = (e: TranscriptEvent): Node | null => {
    if (e.type === "tool_use") {
      const id = typeof e.id === "string" ? e.id : undefined;
      const res = id ? resultById.get(id) : undefined;
      if (res && id) paired.add(id);
      return { event: e, result: res, children: [] };
    }
    if (e.type === "tool_result") {
      const id = typeof e.tool_use_id === "string" ? e.tool_use_id : undefined;
      if (id && paired.has(id)) return null;
      return { event: e, children: [] };
    }
    return { event: e, children: [] };
  };

  events.forEach((e, i) => {
    if (e.type === "result" && dropResult && i === lastResultIdx) return;

    if (e.type === "subagent") {
      const key = str(e.task_id) || str(e.tool_use_id);
      if (key && subagentByKey.has(key)) {
        mergeSubagent(subagentByKey.get(key)!.event, e);
        return;
      }
      const node: Node = { event: { ...e }, children: [] };
      if (key) subagentByKey.set(key, node);
      if (typeof e.tool_use_id === "string") nodesByTuid.set(e.tool_use_id, node);
      out.push(node);
      return;
    }

    // The Task tool call itself is represented by its subagent card.
    if (e.type === "tool_use" && typeof e.id === "string" && subagentTuids.has(e.id)) return;

    const parent = typeof e.parent_tool_use_id === "string" ? e.parent_tool_use_id : undefined;
    if (parent && nodesByTuid.has(parent)) {
      const child = makeToolNode(e);
      if (child) nodesByTuid.get(parent)!.children.push(child);
      return;
    }

    const node = makeToolNode(e);
    if (node) out.push(node);
  });

  return out;
}

export function TranscriptView({ events }: { events: TranscriptEvent[] }) {
  const nodes = useMemo(() => buildTree(events), [events]);
  if (nodes.length === 0) {
    return <p className="px-1 py-6 text-center font-mono text-xs text-moon-600">No output yet.</p>;
  }
  return (
    <div className="space-y-2 font-mono text-[12px] leading-relaxed">
      {nodes.map((n) => (
        <NodeRow key={n.event._key} node={n} />
      ))}
    </div>
  );
}

function NodeRow({ node }: { node: Node }) {
  if (node.event.type === "subagent") return <SubagentCard node={node} />;
  return <EventRow event={node.event} result={node.result} />;
}

function EventRow({ event: e, result }: { event: TranscriptEvent; result?: TranscriptEvent }) {
  switch (e.type) {
    case "assistant_text":
    case "text":
    case "assistant":
      return <AssistantText text={str(e.text)} />;
    case "thinking":
      return <Thinking text={str(e.text)} />;
    case "tool_use":
      if (e.tool === "TaskCreate" || e.tool === "TaskUpdate" || e.tool === "TodoWrite")
        return <TodoMutation event={e} />;
      return <ToolCard event={e} result={result} />;
    case "tool_result":
      return <ToolResult event={e} />;
    case "result":
      return <ResultCard text={str(e.summary || e.text)} />;
    case "worker_error":
    case "cancelled":
      return <ErrorCard text={str(e.message || e.text || e.reason || "Run error")} />;
    case "rate_limit":
      return <MetaLine text="Rate-limit window report" muted />;
    case "user_message":
    case "user":
      return <UserTurn text={str(e.text || e.message)} />;
    case "meta":
    case "system":
    case "preset":
    case "stats":
      return null;
    default:
      return null;
  }
}

// --- Assistant narrative (visual primacy) --------------------------------------

function segments(text: string): { code: boolean; lang: string; body: string }[] {
  if (!text) return [];
  const out: { code: boolean; lang: string; body: string }[] = [];
  let buf: string[] = [];
  let inCode = false;
  let lang = "";
  for (const line of text.split("\n")) {
    const m = line.match(/^```([A-Za-z0-9_+\-.]*)\s*$/);
    if (m) {
      if (inCode) {
        out.push({ code: true, lang, body: buf.join("\n") });
        buf = [];
        inCode = false;
        lang = "";
      } else {
        if (buf.length) out.push({ code: false, lang: "", body: buf.join("\n") });
        buf = [];
        inCode = true;
        lang = m[1] ?? "";
      }
      continue;
    }
    buf.push(line);
  }
  if (buf.length) out.push({ code: inCode, lang, body: buf.join("\n") });
  return out;
}

function AssistantText({ text }: { text: string }) {
  const segs = segments(text);
  if (!text.trim()) return null;
  return (
    <div className="rounded-card border border-ink-700 border-l-2 border-l-lamp/60 bg-ink-900 px-3.5 py-2.5 shadow-[var(--shadow-panel)]">
      {segs.map((s, i) =>
        s.code ? (
          <pre
            key={i}
            className="my-1.5 overflow-x-auto rounded-control border border-ink-700 bg-ink-950 px-2.5 py-2 text-[11px] text-moon-100"
          >
            {s.body}
          </pre>
        ) : (
          <p
            key={i}
            className="whitespace-pre-wrap font-sans text-[13.5px] leading-relaxed text-moon-100"
          >
            {s.body}
          </p>
        ),
      )}
    </div>
  );
}

function Thinking({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (!text.trim()) return null;
  return (
    <details
      className="rounded-control border border-ink-700/40 bg-ink-950/40 px-3 py-1.5"
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer list-none text-[11px] text-moon-600 hover:text-moon-400">
        <ChevronRight size={11} className={cn("mr-1 inline transition-transform", open && "rotate-90")} />
        thinking
      </summary>
      <p className="mt-1 whitespace-pre-wrap text-[11px] italic leading-relaxed text-moon-400">{text}</p>
    </details>
  );
}

// --- Tool calls (muted) --------------------------------------------------------

function ToolCard({ event, result }: { event: TranscriptEvent; result?: TranscriptEvent }) {
  const [open, setOpen] = useState(false);
  const s = toolSummary(event);
  const tone = TOOL_TONE[s.tag] ?? "text-moon-400";
  const resultText = result ? extractResultText(result) : "";
  const isError = Boolean(result?.is_error);

  return (
    <div
      className={cn(
        "overflow-hidden rounded-control border bg-ink-950/40",
        isError ? "border-failed/40" : "border-ink-700/50",
      )}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-ink-800/60"
      >
        <ChevronRight size={12} className={cn("shrink-0 text-moon-600 transition-transform", open && "rotate-90")} />
        {s.tag === "Bash" ? (
          <Terminal size={12} className={cn("shrink-0", tone)} />
        ) : (
          <Wrench size={12} className={cn("shrink-0", tone)} />
        )}
        <span className={cn("shrink-0 font-semibold", tone)}>{s.tag}</span>
        <span className="min-w-0 flex-1 truncate text-moon-400">{s.primary}</span>
        {s.meta && <span className="shrink-0 text-[11px] text-moon-600">{s.meta}</span>}
        {isError && <AlertTriangle size={12} className="shrink-0 text-failed" />}
      </button>
      {open && (
        <div className="border-t border-ink-700/50 bg-ink-950/60 px-3 py-2">
          {Object.keys((event.input as object) ?? {}).length > 0 && (
            <pre className="mb-2 overflow-x-auto text-[11px] text-moon-400">
              {JSON.stringify(event.input, null, 2)}
            </pre>
          )}
          {resultText && (
            <pre
              className={cn(
                "max-h-72 overflow-auto whitespace-pre-wrap text-[11px]",
                isError ? "text-failed" : "text-moon-100",
              )}
            >
              {resultText}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function extractResultText(result: TranscriptEvent): string {
  const c = result.content ?? result.output ?? result.text;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c
      .map((part) =>
        part && typeof part === "object" && "text" in part
          ? String((part as { text: unknown }).text)
          : typeof part === "string"
            ? part
            : "",
      )
      .join("\n");
  }
  return c != null ? JSON.stringify(c, null, 2) : "";
}

function ToolResult({ event }: { event: TranscriptEvent }) {
  const text = extractResultText(event);
  const isError = Boolean(event.is_error);
  if (!text.trim()) return null;
  return (
    <pre
      className={cn(
        "max-h-60 overflow-auto whitespace-pre-wrap rounded-control border bg-ink-950/50 px-3 py-2 text-[11px]",
        isError ? "border-failed/40 text-failed" : "border-ink-700/50 text-moon-400",
      )}
    >
      {text}
    </pre>
  );
}

// --- Todo mutations (inline) ---------------------------------------------------

function TodoMutation({ event }: { event: TranscriptEvent }) {
  const input = (event.input as Record<string, unknown>) ?? {};
  if (event.tool === "TaskCreate") {
    return (
      <div className="flex items-center gap-2 px-2 text-[11px] text-moon-500">
        <ListPlus size={12} className="shrink-0 text-moon-600" />
        <span className="text-moon-600">new task</span>
        <span className="min-w-0 flex-1 truncate text-moon-400">{str(input.subject)}</span>
      </div>
    );
  }
  // TaskUpdate
  const status = str(input.status);
  const tid = str(input.taskId);
  return (
    <div className="flex items-center gap-2 px-2 text-[11px]">
      {status === "completed" ? (
        <CheckCircle2 size={12} className="shrink-0 text-success" />
      ) : status === "in_progress" ? (
        <Loader2 size={12} className="shrink-0 text-lamp" />
      ) : (
        <Circle size={12} className="shrink-0 text-moon-600" />
      )}
      <span className="text-moon-600">task #{tid}</span>
      <span
        className={cn(
          "text-moon-400",
          status === "completed" && "text-success",
          status === "in_progress" && "text-lamp",
        )}
      >
        {status.replace(/_/g, " ")}
      </span>
    </div>
  );
}

// --- Subagent card (expandable, nested children) -------------------------------

function SubagentCard({ node }: { node: Node }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const e = node.event;
  const tuid = str(e.tool_use_id);

  // The Sub-agents rail panel dispatches this to jump to + expand a card.
  useEffect(() => {
    const onFocus = (ev: Event) => {
      if ((ev as CustomEvent).detail !== tuid || !tuid) return;
      setOpen(true);
      ref.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    };
    window.addEventListener("nightdesk:focus-subagent", onFocus);
    return () => window.removeEventListener("nightdesk:focus-subagent", onFocus);
  }, [tuid]);

  const label = (str(e.subagent_type) || str(e.task_type).replace(/_/g, " ") || "subagent").trim();
  const status = str(e.status).toLowerCase();
  const failed = ["failed", "error", "errored"].includes(status);
  const done = str(e.phase) === "notification" || status === "completed";
  const detail = str(e.description) || str(e.last_tool_name);
  const summary = str(e.summary);
  const childCount = node.children.length;

  return (
    <div
      ref={ref}
      data-subagent-id={tuid || undefined}
      className={cn(
        "scroll-mt-4 overflow-hidden rounded-card border bg-review/[0.05]",
        failed ? "border-failed/40" : "border-review/30",
      )}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-review/[0.09]"
        aria-expanded={open}
      >
        <ChevronRight size={12} className={cn("shrink-0 text-moon-600 transition-transform", open && "rotate-90")} />
        <Users size={13} className={cn("shrink-0", failed ? "text-failed" : "text-review")} />
        <span className={cn("shrink-0 font-semibold", failed ? "text-failed" : "text-review")}>{label}</span>
        <span className="min-w-0 flex-1 truncate text-moon-400">{detail}</span>
        {childCount > 0 && <span className="shrink-0 text-[10px] text-moon-600">{childCount} steps</span>}
        {failed ? (
          <AlertTriangle size={12} className="shrink-0 text-failed" />
        ) : done ? (
          <CheckCircle2 size={12} className="shrink-0 text-success" />
        ) : (
          <Loader2 size={12} className="shrink-0 text-moon-600 motion-safe:animate-spin" />
        )}
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-ink-700/40 bg-ink-950/40 px-3 py-2">
          {str(e.prompt) && (
            <details className="text-[11px]">
              <summary className="cursor-pointer text-moon-600 hover:text-moon-400">task prompt</summary>
              <p className="mt-1 whitespace-pre-wrap text-moon-400">{str(e.prompt)}</p>
            </details>
          )}
          {childCount > 0 ? (
            <div className="space-y-1.5 border-l border-ink-700/50 pl-2">
              {node.children.map((c) => (
                <NodeRow key={c.event._key} node={c} />
              ))}
            </div>
          ) : (
            <p className="text-[11px] text-moon-600">
              No nested activity captured in the stream for this sub-agent.
            </p>
          )}
          {summary && (
            <p className="whitespace-pre-wrap rounded-control border border-ink-700/50 bg-ink-900 px-2.5 py-1.5 text-[12px] text-moon-100">
              {summary}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// --- Results, errors, misc -----------------------------------------------------

function ResultCard({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <div className="rounded-card border border-success/30 bg-success/[0.06] px-3.5 py-2.5">
      <p className="whitespace-pre-wrap font-sans text-[13px] leading-relaxed text-moon-100">{text}</p>
    </div>
  );
}

function ErrorCard({ text }: { text: string }) {
  return (
    <div className="rounded-card border border-failed/50 bg-failed/[0.10] px-3.5 py-2.5">
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-failed">
        <AlertTriangle size={13} /> Error
      </div>
      <p className="whitespace-pre-wrap font-sans text-[12.5px] leading-relaxed text-failed">{text}</p>
    </div>
  );
}

function UserTurn({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <div className="rounded-card border border-lamp/25 bg-lamp/[0.05] px-3.5 py-2.5">
      <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-lamp">You</div>
      <p className="whitespace-pre-wrap font-sans text-[13px] text-moon-100">{text}</p>
    </div>
  );
}

function MetaLine({ text, muted }: { text: string; muted?: boolean }) {
  return <p className={cn("px-1 text-[11px]", muted ? "text-moon-600" : "text-moon-400")}>{text}</p>;
}
