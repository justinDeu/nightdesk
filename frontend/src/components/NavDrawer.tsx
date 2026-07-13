import { useEffect, useRef } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import { LogOut, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { useAgentAttention } from "@/api/agents";
import { ENTRIES, signOut } from "./navEntries";
import { ProjectsNavGroup } from "./ProjectsNavGroup";

/** Mobile navigation drawer (below md). Slides in from the left over a scrim;
 *  shares ENTRIES + signOut with the desktop SideNav. Closes on Escape, scrim
 *  click, and any navigation. Focuses the panel on open and restores focus on
 *  close; honors prefers-reduced-motion for the slide. */
export function NavDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  // Attention = structured needs-input + unread replies; shares the Desk
  // band's queryKey so the two surfaces cost one poll.
  const attention = useAgentAttention();
  const pendingCount = attention.data?.total ?? 0;

  // Close whenever the route changes (covers Link taps + programmatic nav).
  useEffect(() => {
    if (open) onClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  // Escape to close; focus the panel on open and restore the trigger on close.
  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      restoreRef.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
      {/* Scrim */}
      <button
        type="button"
        aria-label="Close navigation"
        onClick={onClose}
        className="fade-in absolute inset-0 bg-ink-950/70 backdrop-blur-sm"
      />

      {/* Panel */}
      <div
        ref={panelRef}
        tabIndex={-1}
        className={cn(
          "drawer-in absolute inset-y-0 left-0 flex w-[80vw] max-w-[280px] flex-col overflow-y-auto overscroll-contain",
          "border-r border-ink-700 bg-ink-900 shadow-[var(--shadow-pop)] outline-none",
        )}
      >
        <div className="flex h-14 items-center gap-2.5 px-3.5">
          <span className="dawn-edge grid h-7 w-7 shrink-0 place-items-center rounded-[9px] text-ink-950">
            <span className="font-display text-sm font-bold">n</span>
          </span>
          <span className="font-display text-[15px] font-semibold tracking-tight text-moon-100">
            nightdesk
          </span>
          <button
            onClick={onClose}
            aria-label="Close navigation"
            className="ml-auto grid h-10 w-10 place-items-center rounded-control text-moon-400 hover:bg-ink-800 hover:text-moon-100"
          >
            <X size={18} />
          </button>
        </div>

        <div className="flex-1 space-y-0.5 px-2.5 py-2">
          {ENTRIES.map((entry) => {
            if (entry.group === "projects") {
              return <ProjectsNavGroup key={entry.to} onNavigate={onClose} />;
            }
            const Icon = entry.icon;
            const badge = entry.badgeKey === "agents-pending" ? pendingCount : 0;
            return (
              <Link
                key={entry.to}
                to={entry.to}
                activeOptions={{ exact: entry.exact }}
                onClick={onClose}
                className={cn(
                  "flex min-h-[44px] items-center gap-3 rounded-control px-2.5 py-2 text-sm font-medium",
                  "text-moon-400 transition-colors duration-100 hover:bg-ink-800 hover:text-moon-100",
                )}
                activeProps={{
                  className:
                    "!text-moon-100 bg-ink-800 shadow-[inset_2px_0_0_var(--color-lamp)]",
                }}
              >
                <Icon size={18} className="shrink-0" />
                <span>{entry.label}</span>
                {badge > 0 && (
                  <span className="ml-auto rounded-full bg-lamp/15 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-lamp">
                    {badge}
                  </span>
                )}
              </Link>
            );
          })}
        </div>

        <div className="border-t border-ink-700 p-2.5">
          <button
            onClick={() => {
              onClose();
              void signOut();
            }}
            aria-label="Sign out"
            className="flex min-h-[44px] w-full items-center gap-2.5 rounded-control px-2.5 text-sm text-moon-400 hover:bg-ink-800 hover:text-moon-100"
          >
            <LogOut size={18} className="shrink-0" />
            <span>Sign out</span>
          </button>
        </div>
      </div>
    </div>
  );
}
