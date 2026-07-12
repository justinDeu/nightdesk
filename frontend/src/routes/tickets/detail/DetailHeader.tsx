import { useEffect, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import {
  ArrowLeft,
  Check,
  Copy,
  Cpu,
  MoreHorizontal,
  Pencil,
  Trash2,
} from "lucide-react";
import type { RunOut, TicketOut } from "@/api/types";
import { ticketsApi } from "@/api/tickets";
import { Button } from "@/ui/Button";
import { Input } from "@/ui/Input";
import { StatusPill } from "@/ui/StatusPill";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { Dialog } from "@/ui/Dialog";
import { toast } from "@/ui/Toast";
import { CloneDialog } from "./ConversationDialogs";
import { useTicketActions } from "@/lib/ticketActions";
import { ticketStatusKind, formatUsd, formatTokens } from "@/lib/status";
import { durationBetween } from "@/lib/time";

export function DetailHeader({
  ticket,
  latestRun,
  onSaveTitle,
}: {
  ticket: TicketOut;
  latestRun?: RunOut;
  onSaveTitle: (title: string) => void;
}) {
  const navigate = useNavigate();
  const actions = useTicketActions();
  const [clone, setClone] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const running = ticket.status === "running";

  async function del() {
    setDeleting(true);
    try {
      await ticketsApi.remove(ticket.id);
      toast.success("Ticket deleted");
      navigate({ to: "/tickets" });
    } catch (err) {
      toast.error("Could not delete ticket", { error: err });
      setDeleting(false);
    }
  }

  return (
    <header className="relative shrink-0 border-b border-ink-700 bg-ink-950/70 px-5 py-2.5 backdrop-blur">
      {running && <span aria-hidden className="dawn-edge absolute inset-x-0 top-0 h-[2px]" />}

      {/* Row 1 — identity: back · hash · title (truncates so it never wraps). */}
      <div className="flex items-center gap-2 text-sm">
        <Link
          to="/tickets"
          className="inline-flex shrink-0 items-center gap-1 text-moon-400 hover:text-moon-100"
        >
          <ArrowLeft size={14} /> Tickets
        </Link>
        <span className="shrink-0 font-mono text-xs text-moon-600">{ticket.id.slice(0, 8)}</span>
        <div className="min-w-0 flex-1">
          <TitleEditor ticket={ticket} onSave={onSaveTitle} />
        </div>
      </div>

      {/* Row 2 — state + actions: one status pill, the state transitions, the
          latest-run strip, and the overflow menu share a single line. */}
      <div className="mt-1.5 flex items-center gap-2">
        {running ? (
          <StatusPill status="running" />
        ) : (
          <StatusPill status={ticketStatusKind(ticket.status)} />
        )}
        <Transitions ticket={ticket} actions={actions} />

        <div className="ml-auto flex items-center gap-2">
          {latestRun && (
            <Link
              to="/tickets/$id/runs/$rid"
              params={{ id: ticket.id, rid: latestRun.id }}
              className="hidden shrink-0 items-center gap-2 rounded-control border border-ink-700 bg-ink-900 px-2.5 py-1 font-mono text-[11px] text-moon-400 hover:bg-ink-800 sm:flex"
            >
              {latestRun.model_used && (
                <span className="inline-flex items-center gap-1">
                  <Cpu size={11} /> {latestRun.model_used}
                </span>
              )}
              <span>{durationBetween(latestRun.started_at, latestRun.finished_at)}</span>
              {latestRun.cost_usd != null && (
                <span className="tabular-nums text-lamp">{formatUsd(latestRun.cost_usd)}</span>
              )}
              {(latestRun.input_tokens != null || latestRun.output_tokens != null) && (
                <span>
                  {formatTokens((latestRun.input_tokens ?? 0) + (latestRun.output_tokens ?? 0))} tok
                </span>
              )}
            </Link>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="sm" variant="ghost" aria-label="More actions">
                <MoreHorizontal size={15} />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem icon={<Copy size={14} />} onSelect={() => setClone(true)}>
                Clone ticket…
              </DropdownMenuItem>
              {ticket.status === "draft" && (
                <DropdownMenuItem onSelect={() => actions.sendToInbox(ticket)}>
                  Send to inbox
                </DropdownMenuItem>
              )}
              {ticket.status === "archived" ? (
                <DropdownMenuItem onSelect={() => actions.unarchive(ticket)}>Restore</DropdownMenuItem>
              ) : ticket.status !== "running" ? (
                <DropdownMenuItem onSelect={() => actions.archive(ticket)}>Archive</DropdownMenuItem>
              ) : null}
              <DropdownMenuSeparator />
              <DropdownMenuItem icon={<Trash2 size={14} />} danger onSelect={() => setConfirmDelete(true)}>
                Delete permanently
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      <CloneDialog
        open={clone}
        ticket={ticket}
        onClose={() => setClone(false)}
        onCloned={(id) => navigate({ to: "/tickets/$id", params: { id } })}
      />

      <Dialog
        open={confirmDelete}
        onOpenChange={(o) => !o && setConfirmDelete(false)}
        title="Delete this ticket?"
        description="This permanently removes the ticket and its run history. This cannot be undone — archive instead if you might want it back."
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button variant="danger" loading={deleting} onClick={del}>
              Delete permanently
            </Button>
          </>
        }
      >
        <p className="truncate font-mono text-xs text-moon-400">{ticket.title}</p>
      </Dialog>
    </header>
  );
}

function TitleEditor({ ticket, onSave }: { ticket: TicketOut; onSave: (v: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(ticket.title);
  useEffect(() => setValue(ticket.title), [ticket.title]);

  if (editing) {
    return (
      <div className="flex items-center gap-2">
        <Input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              onSave(value.trim() || ticket.title);
              setEditing(false);
            } else if (e.key === "Escape") {
              setValue(ticket.title);
              setEditing(false);
            }
          }}
          className="min-w-0 flex-1 !text-base font-display font-semibold"
        />
        <Button
          size="sm"
          variant="primary"
          onClick={() => {
            onSave(value.trim() || ticket.title);
            setEditing(false);
          }}
        >
          <Check size={14} />
        </Button>
      </div>
    );
  }
  return (
    <button onClick={() => setEditing(true)} className="group flex min-w-0 items-center gap-2 text-left">
      <h1 className="min-w-0 truncate font-display text-base font-semibold leading-tight tracking-tight text-moon-100">
        {ticket.title}
      </h1>
      <Pencil size={13} className="shrink-0 text-moon-600 opacity-0 group-hover:opacity-100" />
    </button>
  );
}

function Transitions({
  ticket,
  actions,
}: {
  ticket: TicketOut;
  actions: ReturnType<typeof useTicketActions>;
}) {
  switch (ticket.status) {
    case "draft":
      return (
        <>
          <Button size="sm" variant="primary" onClick={() => actions.runNow(ticket)}>
            Run now
          </Button>
          <Button size="sm" variant="ghost" onClick={() => actions.transition(ticket.id, "queued")}>
            Queue
          </Button>
        </>
      );
    case "queued":
      return (
        <>
          <Button size="sm" variant="primary" onClick={() => actions.runNow(ticket)}>
            Run now
          </Button>
          <Button size="sm" variant="ghost" onClick={() => actions.transition(ticket.id, "draft")}>
            Move to draft
          </Button>
        </>
      );
    case "running":
      return (
        <Button size="sm" variant="danger" onClick={() => actions.cancel(ticket)}>
          Cancel run
        </Button>
      );
    case "review":
      return (
        <Button size="sm" variant="primary" onClick={() => actions.requeue(ticket)}>
          Requeue
        </Button>
      );
    case "archived":
      return (
        <Button size="sm" variant="ghost" onClick={() => actions.unarchive(ticket)}>
          Restore
        </Button>
      );
    default:
      return null;
  }
}
