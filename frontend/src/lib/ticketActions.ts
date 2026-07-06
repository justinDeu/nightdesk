import { useQueryClient } from "@tanstack/react-query";
import { qk } from "@/api";
import { ticketsApi } from "@/api/tickets";
import type { TicketOut } from "@/api/types";
import { toast } from "@/ui/Toast";
import { confirm } from "@/ui/confirm";

/**
 * Common ticket lifecycle actions wrapped with a success/failure toast and
 * cache invalidation. Every mutation the UI fires goes through here so the
 * "toast on every mutation" quality bar holds without per-call boilerplate.
 */
export function useTicketActions() {
  const qc = useQueryClient();

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.tickets.all });

  async function run(
    label: string,
    fn: () => Promise<TicketOut | void>,
    okMsg?: string,
  ): Promise<boolean> {
    try {
      await fn();
      invalidate();
      qc.invalidateQueries({ queryKey: ["inbox"] });
      if (okMsg) toast.success(okMsg);
      return true;
    } catch (err) {
      toast.error(`${label} failed`, { error: err });
      return false;
    }
  }

  return {
    requeue: (t: TicketOut) => run("Requeue", () => ticketsApi.requeue(t.id), "Ticket requeued"),
    archive: (t: TicketOut) => run("Archive", () => ticketsApi.archive(t.id), "Ticket archived"),
    unarchive: (t: TicketOut) =>
      run("Unarchive", () => ticketsApi.unarchive(t.id), "Ticket restored"),
    runNow: (t: TicketOut) => run("Run now", () => ticketsApi.runNow(t.id), "Run dispatched"),
    cancelRunNow: (t: TicketOut) =>
      run("Cancel", () => ticketsApi.cancelRunNow(t.id), "Run-now cancelled"),
    // Cancelling an in-flight run is destructive and one click away in four
    // places (running card, detail header, peek, board Move menu). Gate it once
    // here so every entry point gets the same confirm.
    cancel: async (t: TicketOut): Promise<boolean> => {
      const ok = await confirm({
        title: "Cancel this run?",
        body: "The agent stops immediately and the ticket returns to review. Work already done is kept, but the run ends where it is.",
        confirmLabel: "Cancel run",
        cancelLabel: "Keep running",
        danger: true,
      });
      if (!ok) return false;
      return run("Cancel run", () => ticketsApi.cancel(t.id), "Run cancelled");
    },
    promote: (t: TicketOut, target: "draft" | "queued") =>
      run("Promote", () => ticketsApi.promote(t.id, { target }), `Promoted to ${target}`),
    decline: (t: TicketOut) => run("Decline", () => ticketsApi.decline(t.id), "Ticket declined"),
    sendToInbox: (t: TicketOut) =>
      run("Send to inbox", () => ticketsApi.sendToInbox(t.id), "Sent to inbox"),
    setPriority: (t: TicketOut, priority: number) =>
      run("Set priority", () => ticketsApi.setPriority(t.id, priority)),
    setProject: (t: TicketOut, projectId: string | null) =>
      run("Set project", () => ticketsApi.setProject(t.id, projectId)),
    transition: (id: string, status: TicketOut["status"], okMsg?: string) =>
      run(
        "Move",
        () => ticketsApi.transition(id, { status: status as never }),
        okMsg ?? `Moved to ${status}`,
      ),
  };
}

export interface StatusMove {
  label: string;
  run: () => void;
  danger?: boolean;
}

/**
 * The legal status moves for a board card, expressed as ready-to-fire actions.
 * Single source of truth for the touch "Move to…" menu so it never re-derives
 * transition legality — it mirrors the same lifecycle the DetailHeader /
 * TicketPeek quick-action buttons drive (draft/queued can run-now or shuffle,
 * running can cancel, review requeues, everything runnable can archive).
 */
export function ticketStatusMoves(
  t: TicketOut,
  actions: ReturnType<typeof useTicketActions>,
): StatusMove[] {
  switch (t.status) {
    case "draft":
      return [
        { label: "Run now", run: () => actions.runNow(t) },
        { label: "Move to Queued", run: () => actions.transition(t.id, "queued") },
        { label: "Send to inbox", run: () => actions.sendToInbox(t) },
        { label: "Archive", run: () => actions.archive(t) },
      ];
    case "queued":
      return [
        { label: "Run now", run: () => actions.runNow(t) },
        { label: "Move to Draft", run: () => actions.transition(t.id, "draft") },
        { label: "Archive", run: () => actions.archive(t) },
      ];
    case "running":
      return [{ label: "Cancel run", run: () => actions.cancel(t), danger: true }];
    case "review":
      return [
        { label: "Requeue", run: () => actions.requeue(t) },
        { label: "Archive", run: () => actions.archive(t) },
      ];
    case "archived":
      return [{ label: "Restore", run: () => actions.unarchive(t) }];
    default:
      return [];
  }
}
