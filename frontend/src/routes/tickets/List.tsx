import { useQueryClient } from "@tanstack/react-query";
import { Tag } from "lucide-react";
import type { LabelOut, ProfileOut, ProjectOut, RunOut, TicketOut } from "@/api/types";
import { qk } from "@/api";
import { labelsApi } from "@/api/labels";
import { StatusPill } from "@/ui/StatusPill";
import { PriorityPicker, ProjectPicker, LabelPicker } from "@/components/PropertyPickers";
import type { TicketGroup } from "./displayModel";
import { formatUsd } from "@/lib/status";
import { ticketHref } from "@/lib/routes";
import { relativeTime } from "@/lib/time";
import { useTicketActions } from "@/lib/ticketActions";
import { toast } from "@/ui/Toast";
import { cn } from "@/lib/cn";

/** Notable finished-run outcome for a row marker. Success is not notable. */
function listOutcome(run?: RunOut): "failed" | "canceled" | null {
  if (!run || !run.finished_at) return null;
  const s = (run.exit_status ?? "").toLowerCase();
  if (!s || s === "success") return null;
  if (s.includes("cancel")) return "canceled";
  return "failed";
}

export interface ListProps {
  groups: TicketGroup[];
  projects: ProjectOut[];
  profiles: ProfileOut[];
  labels: LabelOut[];
  latestRun: Map<string, RunOut>;
  /** Plain click — open the side peek. */
  onSelect: (id: string) => void;
  /** Explicit open — full navigation. */
  onOpen: (id: string) => void;
  /** Checkbox / x — toggle single selection. */
  onToggleSelect: (id: string) => void;
  /** Shift-click — extend selection to a range. */
  onRangeSelect: (id: string) => void;
  focusedId?: string;
  peekedId?: string;
  selected: Set<string>;
}

export function List({
  groups,
  projects,
  labels,
  latestRun,
  onSelect,
  onOpen,
  onToggleSelect,
  onRangeSelect,
  focusedId,
  peekedId,
  selected,
}: ListProps) {
  const qc = useQueryClient();
  const actions = useTicketActions();
  const anySelected = selected.size > 0;

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.tickets.all });

  async function setLabels(t: TicketOut, ids: string[]) {
    try {
      await labelsApi.setTicketLabels(t.id, ids);
      invalidate();
    } catch (err) {
      toast.error("Could not update labels", { error: err });
    }
  }

  return (
    <div className="relative">
      <div className="overflow-hidden rounded-card border border-ink-700">
        {groups.map((g) => (
          <div key={g.key}>
            <div className="flex items-center gap-2 bg-ink-900 px-3 py-1.5">
              {g.color && (
                <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: g.color }} />
              )}
              <span className="font-display text-[11px] font-semibold uppercase tracking-wide text-moon-400">
                {g.label}
              </span>
              <span className="font-mono text-[10px] text-moon-600">{g.tickets.length}</span>
            </div>
            {g.tickets.map((t) => {
              const run = latestRun.get(t.id);
              const isSel = selected.has(t.id);
              const failed = t.status !== "running" && listOutcome(run) === "failed";
              return (
                <div
                  key={t.id}
                  onClick={(e) =>
                    e.metaKey || e.ctrlKey
                      ? onOpen(t.id)
                      : e.shiftKey
                        ? onRangeSelect(t.id)
                        : onSelect(t.id)
                  }
                  className={cn(
                    "group flex items-center gap-2.5 border-t border-ink-700/60 px-3 py-2 text-sm",
                    "cursor-pointer transition-colors",
                    isSel
                      ? "wash-selected"
                      : failed
                        ? "wash-failed"
                        : "hover:bg-ink-800",
                    peekedId === t.id
                      ? "ring-1 ring-inset ring-lamp/60 bg-ink-800"
                      : focusedId === t.id && "ring-1 ring-inset ring-lamp/40",
                  )}
                >
                  <input
                    type="checkbox"
                    checked={isSel}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (e.shiftKey) {
                        e.preventDefault();
                        onRangeSelect(t.id);
                      }
                    }}
                    onChange={() => onToggleSelect(t.id)}
                    className={cn(
                      "accent-lamp transition-opacity",
                      // Match the board: the checkbox hides until hover, and stays
                      // visible whenever this row (or any row) is selected.
                      isSel || anySelected
                        ? "opacity-100"
                        : "opacity-0 group-hover:opacity-100 focus-visible:opacity-100",
                    )}
                    aria-label={`Select ${t.title}`}
                  />
                  {t.status === "running" ? (
                    <StatusPill status="running" />
                  ) : (
                    <span className="w-1.5" />
                  )}
                  <div onClick={(e) => e.stopPropagation()}>
                    <PriorityPicker
                      value={t.priority}
                      onChange={(v) => actions.setPriority(t, v)}
                    />
                  </div>
                  <a
                    href={ticketHref(t.id)}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (e.metaKey || e.ctrlKey) return;
                      e.preventDefault();
                      if (e.shiftKey) onRangeSelect(t.id);
                      else onSelect(t.id);
                    }}
                    className="min-w-0 flex-1 truncate rounded-[4px] text-moon-100 hover:text-lamp focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp group-hover:text-lamp"
                  >
                    {t.title}
                  </a>
                  {t.labels.length > 0 && (
                    <div className="hidden shrink-0 gap-1 md:flex" onClick={(e) => e.stopPropagation()}>
                      {t.labels.slice(0, 3).map((l) => (
                        <span
                          key={l.id}
                          className="rounded-full px-1.5 py-0.5 text-[10px] font-medium text-ink-950"
                          style={{ backgroundColor: l.color }}
                        >
                          {l.name}
                        </span>
                      ))}
                    </div>
                  )}
                  {/* The add-labels affordance: on a row that already has labels
                      it sits beside the chips; on an unlabeled row it would be a
                      lone dangling icon, so reveal it only on hover/selection. */}
                  <div
                    className={cn(
                      "shrink-0 transition-opacity",
                      t.labels.length > 0
                        ? "opacity-100"
                        : isSel
                          ? "opacity-100"
                          : "opacity-0 group-hover:opacity-100 focus-within:opacity-100",
                    )}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <LabelPicker
                      value={t.labels.map((l) => l.id)}
                      labels={labels}
                      onChange={(ids) => setLabels(t, ids)}
                    >
                      <Tag size={13} className="text-moon-600 hover:text-moon-100" />
                    </LabelPicker>
                  </div>
                  <div className="hidden w-32 shrink-0 md:block" onClick={(e) => e.stopPropagation()}>
                    <ProjectPicker
                      value={t.project_id}
                      projects={projects}
                      onChange={(id) => actions.setProject(t, id)}
                    />
                  </div>
                  {failed && (
                    <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-failed/30 bg-failed/10 px-1.5 py-0.5 text-[10px] font-medium text-failed">
                      <span className="h-1.5 w-1.5 rounded-full bg-failed" /> Failed
                    </span>
                  )}
                  <span className="hidden w-14 shrink-0 text-right font-mono text-[11px] text-moon-600 sm:block">
                    {run?.cost_usd != null ? formatUsd(run.cost_usd) : ""}
                  </span>
                  <span className="hidden w-16 shrink-0 text-right text-[11px] text-moon-600 lg:block">
                    {relativeTime(t.updated_at)}
                  </span>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
