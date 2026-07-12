/**
 * Shared helpers for the project pages (/projects index + /projects/$id).
 *
 * The project pages compose existing, already-project-scoped APIs (tickets,
 * inbox, /projects/{id}/activity, analytics/spend) — see
 * docs/design/v2-project-pages.md. This module holds the pure helpers both
 * pages share: lifecycle ordering, status counting, defaults-strip fragments,
 * and the board deep-link filter token.
 */
import type { ProjectOut, TicketOut } from "@/api/types";
import { parseTs } from "@/lib/time";

/** Triage order for the management queue: what needs you first, sinking to the
 *  backlog. Inbox is surfaced in its own band (it is not board work yet) and
 *  archived is excluded from the queue entirely. */
export const QUEUE_ORDER = ["review", "running", "queued", "draft"] as const;

export const QUEUE_LABEL: Record<string, string> = {
  review: "Review",
  running: "Running",
  queued: "Queued",
  draft: "Draft",
};

/** Human label for a workspace-mode enum (matches domain.workspaces). */
export function workspaceModeLabel(mode: string | null | undefined): string | null {
  switch (mode) {
    case "in_place":
      return "In place";
    case "directory":
      return "Directory";
    case "git_worktree":
      return "Git worktree";
    default:
      return null;
  }
}

export interface DefaultsFragment {
  label: string;
  tooltip: string;
}

/** One-line summary of the project's creation defaults (the old UI's "defaults
 *  strip"). Each fragment is label + tooltip so the page renders styled
 *  Tooltips, never native title=. Empty when the project has no defaults. */
export function defaultsFragments(p: ProjectOut): DefaultsFragment[] {
  const out: DefaultsFragment[] = [];
  const mode = workspaceModeLabel(p.default_workspace_mode);
  if (mode) out.push({ label: mode, tooltip: "Default workspace mode for new tickets" });
  if (p.default_base_ref) out.push({ label: p.default_base_ref, tooltip: "Default base ref for worktrees" });
  const ws = p.default_linked_workspaces?.length ?? 0;
  if (ws > 0) out.push({ label: `${ws} linked workspace${ws === 1 ? "" : "s"}`, tooltip: "Default linked workspaces attached to new tickets" });
  if (p.default_toolchains.length > 0)
    out.push({ label: `${p.default_toolchains.length} toolset${p.default_toolchains.length === 1 ? "" : "s"}`, tooltip: "Default toolsets for new tickets" });
  if (p.default_tool_paths.length > 0)
    out.push({ label: `${p.default_tool_paths.length} tool path${p.default_tool_paths.length === 1 ? "" : "s"}`, tooltip: "Default tool paths for new tickets" });
  return out;
}

/** The board filter token that scopes Tickets to one project (resolved by slug
 *  in routes/tickets/filterModel.ts matchProject). Used by the "Open in board"
 *  deep-links so the project page hands off to the full board experience. */
export function projectFilterToken(p: ProjectOut): string {
  return `project:${p.slug}`;
}

/** The three "needs attention" lifecycle buckets the projects-index rail shows
 *  per project. Counts come from a single cross-project ticket fetch (see
 *  tallyRailCounts) — never one query per card. */
export interface RailCounts {
  running: number;
  review: number;
  inbox: number;
}

/** Tally ONE flat ticket list (every project, fetched in a single
 *  GET /api/v1/tickets call) into per-project running/review/inbox counts.
 *  This is the projects index's grouped query: O(tickets) once, not O(per
 *  project). Only the three attention buckets are counted; queued/draft/
 *  archived are ignored. */
export function tallyRailCounts(tickets: TicketOut[]): Map<string, RailCounts> {
  const m = new Map<string, RailCounts>();
  for (const t of tickets) {
    if (!t.project_id) continue;
    if (t.status !== "running" && t.status !== "review" && t.status !== "inbox") continue;
    let c = m.get(t.project_id);
    if (!c) {
      c = { running: 0, review: 0, inbox: 0 };
      m.set(t.project_id, c);
    }
    c[t.status as "running" | "review" | "inbox"]++;
  }
  return m;
}

export interface StatusCounts {
  review: number;
  running: number;
  queued: number;
  draft: number;
  inbox: number;
  archived: number;
}

/** Tally a flat ticket list into lifecycle buckets. Inbox is counted from the
 *  list when present (the dedicated inbox endpoint is the source of truth for
 *  the inbox band, but the pulse counts off the same project ticket fetch). */
export function countByStatus(tickets: TicketOut[]): StatusCounts {
  const c: StatusCounts = { review: 0, running: 0, queued: 0, draft: 0, inbox: 0, archived: 0 };
  for (const t of tickets) {
    if (t.status in c) c[t.status as keyof StatusCounts]++;
  }
  return c;
}

/** Group a ticket list into the queue's triage order, dropping empty groups. */
export function queueGroups(tickets: TicketOut[]): { status: string; label: string; tickets: TicketOut[] }[] {
  return QUEUE_ORDER.map((status) => ({
    status,
    label: QUEUE_LABEL[status],
    tickets: tickets
      .filter((t) => t.status === status)
      .sort((a, b) => b.priority - a.priority || b.updated_at.localeCompare(a.updated_at)),
  })).filter((g) => g.tickets.length > 0);
}

/** Calendar-day bucket key (YYYY-MM-DD) for a timestamp, in the browser's zone. */
export function dayKey(iso: string | null | undefined): string | null {
  const ms = parseTs(iso);
  if (ms === null) return null;
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** "today" / "yesterday" / "Jul 5" label for a day bucket, relative to now. */
export function dayLabel(key: string): string {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const d = new Date(`${key}T00:00:00`);
  const diff = Math.round((today.getTime() - d.getTime()) / 86_400_000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
