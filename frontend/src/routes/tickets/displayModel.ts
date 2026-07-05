/**
 * Board / list display options — grouping and ordering, matching the old
 * board's display controls. The chosen options persist (like the view toggle)
 * and drive both views. Drag-and-drop stays meaningful only when grouping by
 * status (the columns are the lifecycle); other groupings are read/organize
 * views with drag disabled.
 */
import type { LabelOut, ProfileOut, ProjectOut, RunOut, TicketOut } from "@/api/types";
import { PRIORITY_SCALE } from "@/lib/priority";

export type GroupBy = "status" | "project" | "priority" | "label" | "profile";
export type OrderBy = "manual" | "priority" | "updated" | "created" | "cost";

export const GROUP_BY_OPTIONS: { value: GroupBy; label: string }[] = [
  { value: "status", label: "Status" },
  { value: "project", label: "Project" },
  { value: "priority", label: "Priority" },
  { value: "label", label: "Label" },
  { value: "profile", label: "Profile" },
];

export const ORDER_BY_OPTIONS: { value: OrderBy; label: string }[] = [
  { value: "manual", label: "Manual" },
  { value: "priority", label: "Priority" },
  { value: "updated", label: "Recently updated" },
  { value: "created", label: "Recently created" },
  { value: "cost", label: "Cost" },
];

export interface DisplayContext {
  projects: ProjectOut[];
  labels: LabelOut[];
  profiles: ProfileOut[];
  latestRun: Map<string, RunOut>;
}

export interface TicketGroup {
  key: string;
  label: string;
  color?: string | null;
  /** present only when grouping by status — enables drag + the running edge */
  status?: string;
  tickets: TicketOut[];
}

const STATUS_ORDER = ["draft", "queued", "running", "review"];
const STATUS_LABEL: Record<string, string> = {
  draft: "Draft",
  queued: "Queued",
  running: "Running",
  review: "Review",
};

export function orderComparator(orderBy: OrderBy, latestRun: Map<string, RunOut>) {
  return (a: TicketOut, b: TicketOut): number => {
    switch (orderBy) {
      case "priority":
        return b.priority - a.priority || a.position - b.position;
      case "updated":
        return b.updated_at.localeCompare(a.updated_at);
      case "created":
        return b.created_at.localeCompare(a.created_at);
      case "cost": {
        const ca = latestRun.get(a.id)?.cost_usd ?? -1;
        const cb = latestRun.get(b.id)?.cost_usd ?? -1;
        return cb - ca;
      }
      case "manual":
      default:
        return a.position - b.position || b.priority - a.priority || a.created_at.localeCompare(b.created_at);
    }
  };
}

/** Split tickets into ordered groups per the chosen grouping. Empty groups are
 *  kept for status (stable columns) and dropped for the dynamic groupings. */
export function groupTickets(
  tickets: TicketOut[],
  groupBy: GroupBy,
  orderBy: OrderBy,
  ctx: DisplayContext,
): TicketGroup[] {
  const cmp = orderComparator(orderBy, ctx.latestRun);
  const sorted = [...tickets].sort(cmp);

  if (groupBy === "status") {
    return STATUS_ORDER.map((s) => ({
      key: s,
      label: STATUS_LABEL[s],
      status: s,
      tickets: sorted.filter((t) => t.status === s),
    }));
  }

  if (groupBy === "priority") {
    return [...PRIORITY_SCALE]
      .reverse()
      .map((p) => ({
        key: `p${p.value}`,
        label: p.label,
        tickets: sorted.filter((t) => t.priority === p.value),
      }))
      .filter((g) => g.tickets.length > 0);
  }

  if (groupBy === "project") {
    const groups: TicketGroup[] = ctx.projects.map((p) => ({
      key: p.id,
      label: p.name,
      color: p.color,
      tickets: sorted.filter((t) => t.project_id === p.id),
    }));
    groups.push({ key: "none", label: "No project", tickets: sorted.filter((t) => !t.project_id) });
    return groups.filter((g) => g.tickets.length > 0);
  }

  if (groupBy === "profile") {
    const groups: TicketGroup[] = ctx.profiles.map((p) => ({
      key: p.id,
      label: p.name,
      tickets: sorted.filter((t) => t.profile_id === p.id),
    }));
    groups.push({ key: "none", label: "No profile", tickets: sorted.filter((t) => !t.profile_id) });
    return groups.filter((g) => g.tickets.length > 0);
  }

  // label — a ticket lands in the group of its first label (or "No label").
  const groups: TicketGroup[] = ctx.labels.map((l) => ({
    key: l.id,
    label: l.name,
    color: l.color,
    tickets: sorted.filter((t) => t.labels[0]?.id === l.id),
  }));
  groups.push({ key: "none", label: "No label", tickets: sorted.filter((t) => t.labels.length === 0) });
  return groups.filter((g) => g.tickets.length > 0);
}
