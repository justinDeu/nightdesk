import { useEffect, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import {
  ArrowLeft,
  Check,
  Copy,
  Maximize2,
  MoreHorizontal,
  Pencil,
  Trash2,
} from "lucide-react";
import type { TicketOut } from "@/api/types";
import { ticketsApi } from "@/api/tickets";
import { Button } from "@/ui/Button";
import { Input, Textarea } from "@/ui/Input";
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
import { useUnsavedGuard } from "@/lib/useUnsavedGuard";
import { ticketStatusKind } from "@/lib/status";

export function DetailHeader({
  ticket,
  onSaveTitle,
  onSaveDescription,
}: {
  ticket: TicketOut;
  onSaveTitle: (title: string) => void;
  onSaveDescription: (description: string) => void;
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

      {/* Row 2 — one action cluster on the left: status pill, the state
          transitions, and the overflow menu (clone/archive/delete) sit
          together so every ticket action is in one place. The description
          fills the header gap to the right as grayed, truncated text. */}
      <div className="mt-1.5 flex items-center gap-2">
        {running ? (
          <StatusPill status="running" />
        ) : (
          <StatusPill status={ticketStatusKind(ticket.status)} />
        )}
        <Transitions ticket={ticket} actions={actions} />

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="ghost" aria-label="More actions">
              <MoreHorizontal size={15} />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
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

        <DescriptionHeaderGap ticket={ticket} onSave={onSaveDescription} />
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

/**
 * The ticket description, relocated out of the rail into the header gap between
 * the left action cluster and the right edge. Rendered as grayed, truncated
 * (max two lines) text; clicking opens an editable dialog. Empty description
 * renders nothing — no placeholder. Editing lives in the expand dialog.
 */
function DescriptionHeaderGap({
  ticket,
  onSave,
}: {
  ticket: TicketOut;
  onSave: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const desc = ticket.description ?? "";
  if (!desc.trim()) return null;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="group ml-auto flex min-w-0 max-w-[600px] items-start gap-1.5 py-0.5 text-left"
        aria-label="View and edit description"
      >
        <span className="min-w-0 flex-1 line-clamp-2 text-[12px] leading-snug text-moon-400 group-hover:text-moon-100">
          {desc}
        </span>
        <Maximize2
          size={11}
          className="mt-0.5 shrink-0 text-moon-600 opacity-0 group-hover:opacity-100"
        />
      </button>
      <DescriptionDialog
        open={open}
        onOpenChange={setOpen}
        value={desc}
        onSave={onSave}
      />
    </>
  );
}

function DescriptionDialog({
  open,
  onOpenChange,
  value,
  onSave,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  value: string;
  onSave: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const dirty = editing && draft !== value;
  useUnsavedGuard(dirty);

  const save = () => {
    onSave(draft.trim());
    setEditing(false);
  };
  const cancel = () => {
    setDraft(value);
    setEditing(false);
  };
  // Route blocker + beforeunload don't cover Radix's own close triggers
  // (Escape, overlay, X); confirm those here, same as TicketPeek's dialog.
  const requestClose = (next: boolean) => {
    if (!next && dirty && !window.confirm("Discard your unsaved description changes?")) return;
    onOpenChange(next);
  };
  const close = () => {
    requestClose(false);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={requestClose}
      title="Description"
      description="What is this ticket, and why? Written for a human scanning the board and review."
      size="lg"
      footer={
        editing ? (
          <>
            <Button size="sm" variant="ghost" onClick={cancel}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" onClick={save}>
              Save description
            </Button>
          </>
        ) : (
          <>
            <Button size="sm" variant="ghost" onClick={close}>
              Close
            </Button>
            <Button
              size="sm"
              variant="primary"
              leadingIcon={<Pencil size={13} />}
              onClick={() => setEditing(true)}
            >
              Edit
            </Button>
          </>
        )
      }
    >
      {editing ? (
        <Textarea
          autoFocus
          className="h-[36vh] max-h-[55dvh]"
          placeholder="What is this ticket, and why? Written for a human scanning the board and review."
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
      ) : (
        // Body scrolls internally so the title + footer stay fixed and the
        // dialog never grows past the viewport.
        <div className="max-h-[55dvh] overflow-y-auto overscroll-contain rounded-control border border-ink-700/60 bg-ink-950/60 p-3">
          <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-moon-100">{value}</p>
        </div>
      )}
    </Dialog>
  );
}
