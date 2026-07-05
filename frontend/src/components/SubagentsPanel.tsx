import { useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronRight, Loader2, Users } from "lucide-react";
import type { SubagentSummary } from "@/lib/transcript";
import { cn } from "@/lib/cn";

/** Ticket-level list of the run's sub-agents. Clicking one jumps to and expands
 *  its card in the transcript (via a window event the card listens for). */
export function SubagentsPanel({
  subagents,
  className,
}: {
  subagents: SubagentSummary[];
  className?: string;
}) {
  const [open, setOpen] = useState(true);
  if (subagents.length === 0) return null;

  const jump = (id: string) =>
    window.dispatchEvent(new CustomEvent("nightdesk:focus-subagent", { detail: id }));

  return (
    <div className={cn("rounded-card border border-ink-700 bg-ink-900 shadow-[var(--shadow-raised)]", className)}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        aria-expanded={open}
      >
        <ChevronRight size={13} className={cn("shrink-0 text-moon-600 transition-transform", open && "rotate-90")} />
        <Users size={13} className="shrink-0 text-review" />
        <span className="text-[11px] font-semibold uppercase tracking-wide text-moon-400">Sub-agents</span>
        <span className="font-mono text-[11px] tabular-nums text-moon-600">{subagents.length}</span>
      </button>
      {open && (
        <ul className="max-h-56 space-y-0.5 overflow-y-auto overscroll-contain border-t border-ink-700/60 px-1.5 py-1.5">
          {subagents.map((s) => (
            <li key={s.id}>
              <button
                onClick={() => jump(s.id)}
                className="flex w-full items-center gap-2 rounded-control px-1.5 py-1.5 text-left hover:bg-ink-800"
              >
                {s.failed ? (
                  <AlertTriangle size={12} className="shrink-0 text-failed" />
                ) : s.done ? (
                  <CheckCircle2 size={12} className="shrink-0 text-success" />
                ) : (
                  <Loader2 size={12} className="shrink-0 text-moon-600 motion-safe:animate-spin" />
                )}
                <span className="shrink-0 text-[12px] font-semibold text-review">{s.label}</span>
                <span className="min-w-0 flex-1 truncate text-[11px] text-moon-400">{s.detail}</span>
                {s.steps > 0 && (
                  <span className="shrink-0 font-mono text-[10px] text-moon-600">{s.steps} steps</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
