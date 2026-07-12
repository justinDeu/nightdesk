import { Link } from "@tanstack/react-router";
import { Archive, MoreHorizontal, RotateCcw, Zap } from "lucide-react";
import type { TicketOut } from "@/api/types";
import { Button } from "@/ui/Button";
import { PriorityChip } from "@/components/PriorityChip";
import { UnackedDot, ticketNeedsAck } from "@/components/UnackedDot";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { useTicketActions, ticketStatusMoves } from "@/lib/ticketActions";
import { relativeTime } from "@/lib/time";
import { ticketHref } from "@/lib/routes";
import { queueGroups } from "./shared";
import { cn } from "@/lib/cn";

/**
 * The project page's center: its actionable tickets grouped by lifecycle status
 * in triage order (Review → Running → Queued → Draft), each row carrying the
 * inline lifecycle actions legal from that status. Acting never navigates away
 * — a click opens the app-wide peek; cmd/ctrl-click or Enter opens the ticket
 * full detail. Empty groups are omitted. The full board experience (reorder,
 * bulk, saved views) is one click away via the header link.
 */
export function ManagementQueue({
  tickets,
  boardFilter,
  peekedId,
  onPeek,
  onOpen,
}: {
  tickets: TicketOut[];
  /** Board filter token (project:<slug>) for the "Open all in board" hand-off. */
  boardFilter: string;
  peekedId?: string;
  onPeek: (id: string) => void;
  onOpen: (id: string) => void;
}) {
  const groups = queueGroups(tickets);
  if (groups.length === 0) return null;

  return (
    <section className="overflow-hidden rounded-card border border-ink-700 bg-ink-900 shadow-[var(--shadow-raised)]">
      <div className="flex items-center justify-between border-b border-ink-700 bg-ink-900 px-3.5 py-2">
        <h2 className="font-display text-xs font-semibold uppercase tracking-wide text-moon-400">
          Queue
        </h2>
        <Link
          to="/tickets"
          search={{ f: boardFilter }}
          className="text-[11px] text-moon-400 hover:text-lamp"
        >
          Open all in board →
        </Link>
      </div>

      {groups.map((g) => (
        <div key={g.status}>
          <div className="flex items-center gap-2 border-b border-ink-700/60 bg-ink-950/40 px-3.5 py-1.5">
            <span className="font-display text-[11px] font-semibold uppercase tracking-wide text-moon-400">
              {g.label}
            </span>
            <span className="font-mono text-[10px] text-moon-600">{g.tickets.length}</span>
          </div>
          {g.tickets.map((t) => (
            <QueueRow
              key={t.id}
              ticket={t}
              peeked={peekedId === t.id}
              onPeek={() => onPeek(t.id)}
              onOpen={() => onOpen(t.id)}
            />
          ))}
        </div>
      ))}
    </section>
  );
}

/** The single most likely next action for a status, surfaced as an inline
 *  button so the common case is one click. The full legal set lives in the ⋯
 *  menu (ticketStatusMoves — one source of truth for transition legality). */
function primaryMove(
  status: string,
  actions: ReturnType<typeof useTicketActions>,
  t: TicketOut,
): { label: string; icon: typeof Zap; run: () => void; danger?: boolean } | null {
  switch (status) {
    case "review":
      return { label: "Requeue", icon: RotateCcw, run: () => actions.requeue(t) };
    case "running":
      return { label: "Cancel", icon: Zap, run: () => actions.cancel(t), danger: true };
    case "queued":
    case "draft":
      return { label: "Run now", icon: Zap, run: () => actions.runNow(t) };
    default:
      return null;
  }
}

function QueueRow({
  ticket,
  peeked,
  onPeek,
  onOpen,
}: {
  ticket: TicketOut;
  peeked: boolean;
  onPeek: () => void;
  onOpen: () => void;
}) {
  const actions = useTicketActions();
  const moves = ticketStatusMoves(ticket, actions);
  const primary = primaryMove(ticket.status, actions, ticket);
  const running = ticket.status === "running";

  return (
    <div
      onClick={(e) => (e.metaKey || e.ctrlKey ? onOpen() : onPeek())}
      className={cn(
        "group flex cursor-pointer items-center gap-2.5 border-b border-ink-700/40 px-3.5 py-2.5 transition-colors",
        peeked ? "bg-ink-800 ring-1 ring-inset ring-lamp/50" : "hover:bg-ink-800/60",
        running && "bg-lamp/[0.03]",
      )}
    >
      {running && <span aria-hidden className="dawn-edge h-3 w-[3px] shrink-0 rounded-full" />}
      <a
        href={ticketHref(ticket.id)}
        onClick={(e) => {
          e.stopPropagation();
          if (e.metaKey || e.ctrlKey) return;
          e.preventDefault();
          onPeek();
        }}
        className="min-w-0 flex-1 rounded-[4px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
      >
        <span className="block truncate text-sm font-medium text-moon-100 group-hover:text-lamp">
          {ticket.description?.trim() || ticket.title}
        </span>
        <span className="mt-0.5 flex items-center gap-2 text-[11px] text-moon-600">
          <PriorityChip value={ticket.priority} hideNone />
          {ticketNeedsAck(ticket) && <UnackedDot />}
          <span>{relativeTime(ticket.updated_at)}</span>
        </span>
      </a>

      <div
        className="flex shrink-0 items-center gap-1 opacity-70 transition-opacity group-hover:opacity-100"
        onClick={(e) => e.stopPropagation()}
      >
        {primary && (
          <Button
            size="sm"
            variant={primary.danger ? "danger" : "subtle"}
            leadingIcon={<primary.icon size={13} />}
            onClick={primary.run}
          >
            <span className="hidden lg:inline">{primary.label}</span>
          </Button>
        )}
        {ticket.status === "review" && (
          <Button
            size="sm"
            variant="ghost"
            leadingIcon={<Archive size={13} />}
            onClick={() => actions.archive(ticket)}
            aria-label="Archive"
          >
            <span className="hidden lg:inline">Archive</span>
          </Button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="ghost" aria-label="More actions">
              <MoreHorizontal size={15} />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {moves.map((m) => (
              <DropdownMenuItem key={m.label} danger={m.danger} onSelect={m.run}>
                {m.label}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
