import { forwardRef } from "react";
import { Ban, Check, Zap } from "lucide-react";
import type { ProjectOut, RunOut, TicketOut } from "@/api/types";
import { PriorityChip } from "@/components/PriorityChip";
import { ProjectTag } from "@/components/ProjectDot";
import { formatUsd } from "@/lib/status";
import { ticketHref } from "@/lib/routes";
import { cn } from "@/lib/cn";

/** Notable finished-run outcome for a card badge/edge. Success is not notable. */
function runOutcome(run?: RunOut): "failed" | "canceled" | null {
  if (!run || !run.finished_at) return null;
  const s = (run.exit_status ?? "").toLowerCase();
  if (!s || s === "success") return null;
  if (s.includes("cancel")) return "canceled";
  return "failed";
}

export interface BoardCardProps {
  ticket: TicketOut;
  project?: ProjectOut;
  latestRun?: RunOut;
  /** Plain click — select + open the side peek. */
  onSelect: () => void;
  /** Explicit open — full navigation (cmd/ctrl/middle-click). */
  onOpen: () => void;
  /** Shift-click — extend the multi-selection to a range. */
  onRangeSelect: () => void;
  /** Click the corner glyph — toggle this card in/out of the selection. */
  onToggleSelect: () => void;
  dragging?: boolean;
  /** cursor focus (keyboard navigation) — reads as a ring ON TOP of selection. */
  focused?: boolean;
  /** currently shown in the side peek */
  peeked?: boolean;
  /** included in the multi-selection — reads as tint + left edge + corner check */
  selected?: boolean;
  /** Suppress the project tag when the board is already grouped by project. */
  hideProject?: boolean;
}

export const BoardCard = forwardRef<HTMLDivElement, BoardCardProps>(function BoardCard(
  { ticket, project, latestRun, onSelect, onOpen, onRangeSelect, onToggleSelect, dragging, focused, peeked, selected, hideProject },
  ref,
) {
  const running = ticket.status === "running";
  const outcome = running ? null : runOutcome(latestRun);

  return (
    <div
      ref={ref}
      onClick={(e) =>
        e.metaKey || e.ctrlKey ? onOpen() : e.shiftKey ? onRangeSelect() : onSelect()
      }
      data-ticket-id={ticket.id}
      className={cn(
        "group relative shrink-0 cursor-pointer overflow-hidden rounded-card border p-3 pt-3.5",
        "shadow-[var(--shadow-raised)] transition-colors",
        // Selection: a lamp fill + left edge (orthogonal to the focus ring).
        // Failed: a left-anchored ember gradient wash + ember-tinted border so
        // the whole card silhouette reads warm and pops out of a column (the pill
        // names it). When a failed card is also selected, keep the ember gradient
        // and let the lamp left edge sit cleanly on top of it.
        selected
          ? outcome === "failed"
            ? "wash-failed border-l-2 border-l-lamp border-y-failed/35 border-r-failed/35 bg-ink-900"
            : "border-l-2 border-l-lamp border-y-ink-700 border-r-ink-700 bg-lamp/[0.07]"
          : outcome === "failed"
            ? "wash-failed border-failed/35 bg-ink-900"
            : "border-ink-700 bg-ink-900 hover:bg-ink-800",
        // Cursor/focus: a strong ring that sits on top of any selection tint.
        focused ? "ring-2 ring-lamp" : peeked ? "ring-1 ring-lamp/50" : "",
        dragging && "opacity-40",
      )}
    >
      {/* Running keeps the animated dawn edge; failed is carried by the wash +
          pill below (no top line — it read as bolted-on). */}
      {running && <span aria-hidden className="dawn-edge absolute inset-x-0 top-0 h-[2px]" />}

      {/* Corner select toggle: always visible once selected; on hover it shows a
          faint outline target so the mouse has an obvious way to (de)select. */}
      <button
        type="button"
        aria-label={selected ? "Deselect ticket" : "Select ticket"}
        aria-pressed={selected}
        onClick={(e) => {
          e.stopPropagation();
          onToggleSelect();
        }}
        className={cn(
          "absolute right-1.5 top-1.5 grid h-4 w-4 place-items-center rounded-full transition",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp",
          selected
            ? "bg-lamp text-ink-950"
            : "border border-moon-600/70 text-transparent opacity-0 hover:border-lamp hover:text-lamp group-hover:opacity-100",
        )}
      >
        <Check size={11} strokeWidth={3} />
      </button>

      <div className="mb-2 flex items-start gap-2">
        {/* Real anchor so middle/cmd-click open a new tab natively; plain click
            still opens the peek (preventDefault). draggable=false keeps the
            card's pragmatic-DnD drag intact instead of dragging the link. */}
        <a
          href={ticketHref(ticket.id)}
          draggable={false}
          onClick={(e) => {
            e.stopPropagation();
            if (e.metaKey || e.ctrlKey) return;
            e.preventDefault();
            if (e.shiftKey) onRangeSelect();
            else onSelect();
          }}
          className="block min-w-0 flex-1 rounded-[4px] text-sm font-medium leading-snug text-moon-100 hover:text-lamp focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp group-hover:text-lamp"
        >
          <span className="line-clamp-3">{ticket.title}</span>
        </a>
        {!selected && (
          // pointer-events-none: once faded on hover this span still overlaps the
          // corner select glyph; without it the click lands here (opening the
          // peek) instead of on the glyph (E2E BUG-2).
          <span className="pointer-events-none transition-opacity group-hover:opacity-0">
            <PriorityChip value={ticket.priority} hideNone />
          </span>
        )}
      </div>

      {ticket.labels.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1">
          {ticket.labels.slice(0, 4).map((l) => (
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

      <div className="flex items-center justify-between gap-2 text-[11px] text-moon-600">
        {hideProject ? (
          <span className="min-w-0" />
        ) : (
          <ProjectTag project={project} showNone className="min-w-0" />
        )}
        <div className="flex shrink-0 items-center gap-2">
          {outcome === "failed" && (
            <span className="inline-flex items-center gap-1 rounded-full border border-failed/30 bg-failed/10 px-1.5 py-0.5 text-[10px] font-medium text-failed">
              <span className="h-1.5 w-1.5 rounded-full bg-failed" /> Failed
            </span>
          )}
          {outcome === "canceled" && (
            <span className="inline-flex items-center gap-0.5 rounded-full bg-ink-800 px-1.5 py-0.5 text-[10px] font-medium text-moon-400">
              <Ban size={9} /> canceled
            </span>
          )}
          {ticket.run_now && (
            <span className="inline-flex items-center gap-0.5 rounded-full bg-lamp/12 px-1.5 py-0.5 font-mono text-[10px] text-lamp">
              <Zap size={9} /> now
            </span>
          )}
          {latestRun?.cost_usd != null && (
            <span className="font-mono tabular-nums">{formatUsd(latestRun.cost_usd)}</span>
          )}
        </div>
      </div>
    </div>
  );
});
