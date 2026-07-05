import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { cn } from "@/lib/cn";

export interface ErrorStateProps {
  title: string;
  description?: ReactNode;
  /** Recovery action — an error state should always offer a way out. */
  action?: ReactNode;
  /** Override the default warning glyph. */
  icon?: ReactNode;
  className?: string;
}

/** Designed error surface — the same centered family as EmptyState, tinted
 *  ember so a failure reads as a failure rather than bare red text in an empty
 *  viewport. Used for not-found pages, query errors, and the router boundaries. */
export function ErrorState({ title, description, action, icon, className }: ErrorStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-card border border-dashed border-failed/30",
        "wash-failed bg-ink-900/40 px-6 py-14 text-center",
        className,
      )}
    >
      <div className="flex h-11 w-11 items-center justify-center rounded-card border border-failed/40 bg-failed/10 text-failed">
        {icon ?? <AlertTriangle size={18} />}
      </div>
      <div className="space-y-1">
        <h3 className="font-display text-sm font-semibold text-moon-100">{title}</h3>
        {description && (
          <p className="mx-auto max-w-sm text-sm text-moon-400">{description}</p>
        )}
      </div>
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}
