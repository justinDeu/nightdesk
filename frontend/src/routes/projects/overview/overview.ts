/**
 * Pure helpers for the project Overview ledger
 * (docs/design/project-control-plane.md §Overview). Shared by the signal strip,
 * the verdict / waiting / inbox sections, and the footer — kept here so the
 * section components stay presentational and the shape derivations have one home.
 */
import type {
  AgentPendingItem,
  InboxItemOut,
  TicketOut,
} from "@/api/types";
import { parseTs } from "@/lib/time";

/** 48h in ms — the staleness threshold for the inbox triage band. */
const STALE_MS = 48 * 60 * 60 * 1000;

/** Is an inbox item stale (older than 48h since creation)? */
export function isInboxStale(item: InboxItemOut, now = Date.now()): boolean {
  const ms = parseTs(item.ticket.created_at);
  return ms !== null && now - ms > STALE_MS;
}

/** An inbox item is "blocked" when it carries blockers, "stale" past 48h.
 *  Either lifts it to the top of the band and swaps its verbs for a single
 *  amber Triage affordance (it can't be queued as-is). */
export function inboxTriaged(item: InboxItemOut, now = Date.now()): boolean {
  return item.blockers.length > 0 || isInboxStale(item, now);
}

/** Sort inbox items: blocked/stale first (amber band), then newest first. */
export function sortInbox(items: InboxItemOut[], now = Date.now()): InboxItemOut[] {
  return [...items].sort((a, b) => {
    const ta = inboxTriaged(a, now) ? 1 : 0;
    const tb = inboxTriaged(b, now) ? 1 : 0;
    if (ta !== tb) return tb - ta; // triaged first
    return (parseTs(b.ticket.created_at) ?? 0) - (parseTs(a.ticket.created_at) ?? 0);
  });
}

/** One-line summary of an agent's pending-input question, for the waiting feed.
 *  Mirrors the PendingInputCard's per-kind extraction but flattened to a string. */
export function pendingQuestion(p: AgentPendingItem): string {
  const payload = p.payload ?? {};
  if (p.kind === "ask_question") {
    const raw = payload.questions;
    const arr = Array.isArray(raw) ? raw : raw ? [raw] : [];
    const first = (arr[0] as Record<string, unknown> | undefined) ?? {};
    return String(first.question ?? first.prompt ?? "The agent asked a question.");
  }
  if (p.kind === "plan_approval") {
    return "Approve the proposed plan?";
  }
  // permission
  return `Allow the agent to use ${p.tool ?? "a tool"}?`;
}

/** The quick-reply decision implied by a pending kind when the human types a
 *  free-text answer: questions/permissions "allow", plan approvals "approve". */
export function pendingQuickDecision(p: AgentPendingItem): "allow" | "approve" {
  return p.kind === "plan_approval" ? "approve" : "allow";
}

/** Elapsed wait label for a pending item ("2h", "3d") — empty when unknown. */
export function pendingWaitLabel(p: AgentPendingItem, now = Date.now()): string {
  const ms = parseTs(p.created_at);
  if (ms === null) return "";
  const s = Math.round((now - ms) / 1000);
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.round(h / 24)}d`;
}

/** Section anchor ids shared by the signal strip jump-links and the sections. */
export const ANCHORS = {
  running: "ov-running",
  verdict: "ov-verdict",
  ack: "ov-ack",
  waiting: "ov-waiting",
  inbox: "ov-inbox",
  footer: "ov-footer",
} as const;

/** "today" / "yesterday" / "Mon D" label for an ack digest group's day key.
 *  Mirrors the Desk's ToAcknowledgeBand so the two ledgers read the same. */
export function ackDayLabel(day: string): string {
  const d = new Date(`${day}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((today.getTime() - d.getTime()) / 86_400_000);
  if (diff === 0) return "today";
  if (diff === 1) return "yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Smooth-scroll a section anchor into view (signal-strip jump links). */
export function jumpTo(anchor: string) {
  document.getElementById(anchor)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** Sort review tickets: unacked first, then priority desc, then most recent. */
export function sortReview(tickets: TicketOut[]): TicketOut[] {
  return [...tickets].sort((a, b) => {
    const ua = a.acknowledged_at ? 0 : 1;
    const ub = b.acknowledged_at ? 0 : 1;
    if (ua !== ub) return ub - ua;
    if (b.priority !== a.priority) return b.priority - a.priority;
    return (parseTs(b.updated_at) ?? 0) - (parseTs(a.updated_at) ?? 0);
  });
}
