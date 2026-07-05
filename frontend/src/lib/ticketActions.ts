import { useQueryClient } from "@tanstack/react-query";
import { ApiError, qk } from "@/api";
import { ticketsApi } from "@/api/tickets";
import type { TicketOut } from "@/api/types";
import { toast } from "@/ui/Toast";

function errMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message || fallback;
  return fallback;
}

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
      toast.error(errMessage(err, `${label} failed`));
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
    cancel: (t: TicketOut) => run("Cancel run", () => ticketsApi.cancel(t.id), "Run cancelled"),
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
