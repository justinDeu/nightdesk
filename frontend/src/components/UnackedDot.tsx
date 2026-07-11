import type { TicketOut } from "@/api/types";
import { Tooltip } from "@/ui/Tooltip";
import { cn } from "@/lib/cn";

/** A ticket carries acknowledgement debt: it settled into review/archived and
 *  no human has stamped the outcome as seen (mirrors the digest criteria in
 *  domain/ack.py — DIGEST_STATUSES + acknowledged_at IS NULL). */
export function ticketNeedsAck(t: TicketOut): boolean {
  return (t.status === "review" || t.status === "archived") && t.acknowledged_at == null;
}

/** Muted "unacked" marker for ticket rows and cards — a reminder, not an
 *  alarm. This is how project-less tickets stay discoverable outside the Desk
 *  digest. Clicking it is NOT an ack; acknowledging happens from the ticket
 *  side-peek or the Desk band. */
export function UnackedDot({ className }: { className?: string }) {
  return (
    <Tooltip content="Agent-completed work you haven't acknowledged">
      <span
        aria-label="Unacknowledged"
        className={cn(
          "inline-flex shrink-0 items-center gap-1 rounded-full bg-ink-800 px-1.5 py-0.5 text-[10px] font-medium text-moon-500",
          className,
        )}
      >
        <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-moon-500/80" />
        unacked
      </span>
    </Tooltip>
  );
}
