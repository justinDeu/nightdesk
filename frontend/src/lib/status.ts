import type { StatusKind } from "@/ui/StatusPill";

/** Map a ticket lifecycle status string to a StatusPill kind. */
export function ticketStatusKind(status: string): StatusKind {
  switch (status) {
    case "draft":
    case "queued":
    case "running":
    case "review":
    case "archived":
      return status;
    default:
      return "draft";
  }
}

/** Map a run exit_status to a StatusPill kind. Unfinished runs are "running".
 *  Canceled runs get their own warning (amber) kind so they never read as an
 *  actual failure — matched against "cancel" rather than an exact string
 *  since the API uses "cancelled"/"canceled" interchangeably. */
export function runStatusKind(exitStatus: string | null | undefined): StatusKind {
  if (!exitStatus) return "running";
  if (exitStatus === "success") return "success";
  if (exitStatus.toLowerCase().includes("cancel")) return "canceled";
  return "failed";
}

const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

/** Format USD for the mono cost chips. Money is always two decimals with
 *  thousands separators; a nonzero amount that would round to $0.00 shows
 *  "<$0.01" so sub-cent spend still reads as real rather than free. */
export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "$0.00";
  if (value > 0 && value < 0.005) return "<$0.01";
  return USD.format(value);
}

/** Compact token counts: 12400 -> "12.4k". */
export function formatTokens(n: number | null | undefined): string {
  if (!n) return "0";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}
