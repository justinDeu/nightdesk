import { cn } from "@/lib/cn";

/** The union covers lifecycle states (draft/queued/running/review/archived) plus
 *  run outcomes (success/failed) so one component labels tickets and runs alike. */
export type StatusKind =
  | "draft"
  | "queued"
  | "running"
  | "review"
  | "archived"
  | "success"
  | "failed";

interface Meta {
  label: string;
  /** static (non-running) pill classes */
  cls: string;
  dot: string;
}

const META: Record<StatusKind, Meta> = {
  draft: { label: "Draft", cls: "text-moon-600 border-ink-700 bg-ink-800", dot: "bg-moon-600" },
  queued: { label: "Queued", cls: "text-queued border-queued/30 bg-queued/10", dot: "bg-queued" },
  running: { label: "Running", cls: "", dot: "" },
  review: { label: "Review", cls: "text-review border-review/30 bg-review/10", dot: "bg-review" },
  archived: {
    label: "Archived",
    cls: "text-moon-400 border-ink-700 bg-ink-800",
    dot: "bg-moon-400",
  },
  success: {
    label: "Success",
    cls: "text-success border-success/30 bg-success/10",
    dot: "bg-success",
  },
  failed: { label: "Failed", cls: "text-failed border-failed/30 bg-failed/10", dot: "bg-failed" },
};

export interface StatusPillProps {
  status: StatusKind;
  className?: string;
  /** Override the default label text. */
  label?: string;
}

export function StatusPill({ status, className, label }: StatusPillProps) {
  const meta = META[status];
  const text = label ?? meta.label;

  if (status === "running") {
    // The running pill IS the dawn edge: a jade→mint gradient hairline border
    // drifting slowly, with a pulsing dot. Static under reduced motion.
    return (
      <span
        className={cn(
          "relative inline-flex items-center gap-1.5 rounded-full p-[1px]",
          className,
        )}
      >
        <span aria-hidden className="dawn-edge absolute inset-0 rounded-full" />
        <span className="relative inline-flex items-center gap-1.5 rounded-full bg-ink-900 px-2 py-0.5 text-[11px] font-medium leading-none">
          <span className="h-1.5 w-1.5 rounded-full bg-lamp motion-safe:animate-pulse" />
          <span className="dawn-text">{text}</span>
        </span>
      </span>
    );
  }

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5",
        "text-[11px] font-medium leading-none whitespace-nowrap",
        meta.cls,
        className,
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", meta.dot)} />
      {text}
    </span>
  );
}
