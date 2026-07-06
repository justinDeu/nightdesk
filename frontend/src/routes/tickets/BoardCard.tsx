import { forwardRef } from "react";
import { Ban, Check, MoreHorizontal, Zap } from "lucide-react";
import type { ProjectOut, RunOut, TicketOut } from "@/api/types";
import { PriorityChip } from "@/components/PriorityChip";
import { ProjectTag } from "@/components/ProjectDot";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { formatUsd } from "@/lib/status";
import { ticketHref } from "@/lib/routes";
import { useTicketActions, ticketStatusMoves } from "@/lib/ticketActions";
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
  const actions = useTicketActions();
  const moves = ticketStatusMoves(ticket, actions);

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
        // Selection: a left-anchored jade wash (.wash-selected), background-only so
        // the 1px border stays uniform on every side in every state and the card
        // never shifts/shrinks a pixel when (de)selected.
        // Failed: a left-anchored ember gradient wash + ember-tinted border so the
        // whole card silhouette reads warm and pops out of a column (the pill names
        // it). Selected + failed: the jade selection wash wins, but the ember border
        // is retained so the failed identity survives in the silhouette.
        selected
          ? outcome === "failed"
            ? "wash-selected border-failed/35 bg-ink-900"
            : "wash-selected border-ink-700 bg-ink-900"
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

      {/* Corner select toggle: a low-emphasis outline target that sits at rest
          (so touch users can always (de)select) and brightens on hover/focus;
          fully lit once selected. The 24px button is a comfortable tap target;
          the inner span carries the small visual glyph so the footprint stays
          tight. */}
      <button
        type="button"
        aria-label={selected ? "Deselect ticket" : "Select ticket"}
        aria-pressed={selected}
        onClick={(e) => {
          e.stopPropagation();
          onToggleSelect();
        }}
        className={cn(
          "absolute right-0.5 top-0.5 grid h-6 w-6 place-items-center rounded-full transition",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp",
          selected
            ? "opacity-100"
            : "opacity-60 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100",
        )}
      >
        <span
          className={cn(
            "grid h-4 w-4 place-items-center rounded-full transition",
            selected
              ? "bg-lamp text-ink-950"
              : "border border-moon-600/70 text-transparent hover:border-lamp hover:text-lamp",
          )}
        >
          <Check size={11} strokeWidth={3} />
        </span>
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
        {/* Kept mounted in every state (only its opacity changes) so selecting or
            hovering never removes it from the flex row and reflows the title — that
            unmount was the "card shrinks slightly on select" regression. Faded out
            when selected (the corner check owns that spot) or on hover.
            pointer-events-none: once faded this span still overlaps the corner
            select glyph; without it the click lands here (opening the peek) instead
            of on the glyph (E2E BUG-2). */}
        <span
          className={cn(
            "pointer-events-none transition-opacity",
            selected ? "opacity-0" : "group-hover:opacity-0",
          )}
        >
          <PriorityChip value={ticket.priority} hideNone />
        </span>
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
          {/* Touch-safe status move: HTML5 drag can't work on a touchscreen, so
              every card carries an overflow menu of its legal transitions. Shown
              at rest on coarse pointers, hover/focus-gated on a mouse where drag
              already works. */}
          {moves.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  aria-label="Move ticket"
                  onClick={(e) => e.stopPropagation()}
                  className={cn(
                    "-mr-1 grid h-6 w-6 shrink-0 place-items-center rounded-control text-moon-400 transition",
                    "hover:bg-ink-800 hover:text-moon-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp",
                    "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100",
                  )}
                >
                  <MoreHorizontal size={15} />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {moves.map((m) => (
                  <DropdownMenuItem key={m.label} danger={m.danger} onSelect={m.run}>
                    {m.label}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </div>
    </div>
  );
});
