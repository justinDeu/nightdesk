import { forwardRef } from "react";
import { Ban, Bot, Check, MoreHorizontal, Zap } from "lucide-react";
import type { ExternalLinkOut, ProjectOut, RunOut, TicketOut } from "@/api/types";
import { MrChip } from "@/components/ExternalItemChips";
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
  /** Highest-signal external link for this ticket (a produced MR), rendered as a
   *  compact chip on the meta row. Populated once a batched external-link feed
   *  exists (v2, see docs/design/gitlab-jira-integrations.md §8); undefined in v1. */
  mrLink?: ExternalLinkOut;
}

export const BoardCard = forwardRef<HTMLDivElement, BoardCardProps>(function BoardCard(
  { ticket, project, latestRun, onSelect, onOpen, onRangeSelect, onToggleSelect, dragging, focused, peeked, selected, hideProject, mrLink },
  ref,
) {
  const running = ticket.status === "running";
  const outcome = running ? null : runOutcome(latestRun);
  const actions = useTicketActions();
  const moves = ticketStatusMoves(ticket, actions);

  // Selection / peek / keyboard-cursor all render as ONE fused edge: the card's
  // own 1px border and the box-shadow ring share a single hue so they never read
  // as two separate lines a pixel apart. The keyboard cursor (focused) forces the
  // jade hue even on a failed card so its edge stays one color; failed cards
  // otherwise carry the ember hue, everything else jade.
  const edgeHue = outcome === "failed" && !focused ? "failed" : "lamp";
  const emphasis = focused || selected ? "strong" : peeked ? "soft" : "none";

  return (
    <div
      ref={ref}
      onClick={(e) =>
        e.metaKey || e.ctrlKey ? onOpen() : e.shiftKey ? onRangeSelect() : onSelect()
      }
      data-ticket-id={ticket.id}
      className={cn(
        "cv-card group relative shrink-0 cursor-pointer overflow-hidden rounded-card border bg-ink-900 p-3 pt-3.5",
        "shadow-[var(--shadow-raised)] transition-colors",
        // Left-anchored wash (background-only, so geometry never shifts on select):
        // failed cards keep the ember wash even when selected so a red card never
        // turns green; a selected non-failed card gets the jade wash.
        outcome === "failed" ? "wash-failed" : selected ? "wash-selected" : "",
        // Fused edge = border hue + ring hue always match (see edgeHue/emphasis).
        //  strong (selected / keyboard cursor): full-strength border + a 3px ring
        //    in the SAME hue → one solid ~4px edge, no two-tone gap.
        //  soft (side-peek only): a tinted border + a 1px ring in the same hue.
        //  none: resting border (ember tint for failed, neutral otherwise).
        emphasis === "strong"
          ? edgeHue === "failed"
            ? "border-failed ring-[3px] ring-failed"
            : "border-lamp ring-[3px] ring-lamp"
          : emphasis === "soft"
            ? edgeHue === "failed"
              ? "border-failed/60 ring-1 ring-failed/50"
              : "border-lamp/55 ring-1 ring-lamp/50"
            : outcome === "failed"
              ? "border-failed/35"
              : "border-ink-700 hover:bg-ink-800",
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

      {/* Title owns the full width now; pr-7 keeps the 3-line clamp clear of the
          corner select toggle (absolute, top-right, 24px). Priority moved to the
          meta row so nothing crowds the checkbox during multi-select. */}
      <div className="mb-2 pr-7">
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
          className="block min-w-0 rounded-[4px] text-sm font-medium leading-snug text-moon-100 hover:text-lamp focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp group-hover:text-lamp"
        >
          <span className="line-clamp-3">{ticket.title}</span>
        </a>
        {/* Human-facing what/why. A short snippet where it fits — the prompt
            (agent instructions) is never shown on the card. */}
        {ticket.description?.trim() && (
          <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-moon-500">
            {ticket.description}
          </p>
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
        {/* Left meta cluster: project + priority ride together here (priority left
            the title row so it can't crowd the corner checkbox). min-w-0 lets the
            project name truncate before the priority chip. */}
        <div className="flex min-w-0 items-center gap-1.5">
          {!hideProject && <ProjectTag project={project} showNone className="min-w-0" />}
          <PriorityChip value={ticket.priority} hideNone className="shrink-0" />
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {mrLink && <MrChip link={mrLink} />}
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
          {/* A run/agent — not a human — moved this into review: it's finished
              pending your awareness, not awaiting a decision. */}
          {ticket.status === "review" && ticket.agent_reviewed && (
            <span className="inline-flex items-center gap-0.5 rounded-full bg-ink-800 px-1.5 py-0.5 text-[10px] font-medium text-moon-400">
              <Bot size={9} /> agent
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
