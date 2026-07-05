import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  /** The next action — an empty state must always point somewhere. */
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-card border border-dashed border-ink-700",
        "bg-ink-900/40 px-6 py-14 text-center",
        className,
      )}
    >
      {icon && (
        <div className="flex h-11 w-11 items-center justify-center rounded-card border border-ink-700 bg-ink-800 text-moon-400">
          {icon}
        </div>
      )}
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
