import { cn } from "@/lib/cn";
import type { AgentLiveness } from "@/api/types";

/** The five-state (plus crashed) visual model, derived server-side by
 *  describe_liveness. See resident-agents-v3.md §5. */
interface Meta {
  label: string;
  cls: string;
  dot: string;
  /** pulse the dot to signal genuine activity */
  pulse?: boolean;
}

const META: Record<AgentLiveness, Meta> = {
  alive: { label: "Alive", cls: "text-success border-success/30 bg-success/10", dot: "bg-success", pulse: true },
  "needs-input": { label: "Needs you", cls: "text-lamp border-lamp/40 bg-lamp/12", dot: "bg-lamp", pulse: true },
  warm: { label: "Warm", cls: "text-dawn border-dawn/30 bg-dawn/10", dot: "bg-dawn" },
  cold: { label: "Cold", cls: "text-moon-400 border-ink-700 bg-ink-800", dot: "bg-moon-500" },
  ended: { label: "Ended", cls: "text-moon-600 border-ink-700 bg-ink-800", dot: "bg-moon-600" },
  crashed: { label: "Crashed", cls: "text-failed border-failed/30 bg-failed/10", dot: "bg-failed" },
};

export function AgentStatePill({
  liveness,
  className,
}: {
  liveness: AgentLiveness;
  className?: string;
}) {
  const meta = META[liveness] ?? META.cold;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5",
        "text-[11px] font-medium leading-none whitespace-nowrap",
        meta.cls,
        className,
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", meta.dot, meta.pulse && "motion-safe:animate-pulse")} />
      {meta.label}
    </span>
  );
}
