import { useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";

/** A `SettingsCard` sibling that starts collapsed for rarely-touched groups
 *  (env, run-token scopes). Collapsing never hides unsaved edits: pass `dirty`
 *  and a lamp dot renders next to the title whenever the card is closed and
 *  its fields differ from the saved baseline. */
export function CollapsibleCard({
  title,
  description,
  children,
  defaultOpen = false,
  dirty = false,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  dirty?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={cn("rounded-card border border-ink-700 bg-ink-900", className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="font-display text-sm font-semibold text-moon-100">{title}</h3>
            {!open && dirty && (
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full bg-lamp"
                aria-label="Unsaved changes in this section"
              />
            )}
          </div>
          {description && <p className="mt-0.5 text-xs text-moon-400">{description}</p>}
        </div>
        <ChevronDown
          size={16}
          className={cn("shrink-0 text-moon-600 transition-transform", open && "rotate-180")}
        />
      </button>
      {open && <div className="border-t border-ink-700/70 px-4 py-4">{children}</div>}
    </div>
  );
}
