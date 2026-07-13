import { useState } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import { ChevronRight, FolderKanban } from "lucide-react";
import { cn } from "@/lib/cn";
import { ProjectDot } from "@/components/ProjectDot";
import { useProjectAttention } from "@/api/projects";

/** Persisted expand/collapse state for the sidebar's Projects group (the
 *  project list is collapsible — docs/design/project-control-plane.md §Chrome). */
const EXPAND_KEY = "nightdesk:projects-nav-expanded";

/**
 * The expandable Projects nav group: a toggle header plus the active projects,
 * each with its color dot, name, needs-you badge, and a lamp pulse when running.
 * Ordered by attention (the rollup is already display-ordered). Shared by the
 * desktop SideNav (expanded rail) and the mobile NavDrawer.
 *
 * The collapsed desktop rail does NOT use this — at 60px there's no room for a
 * sub-list, so SideNav renders a plain Projects icon link there instead.
 */
export function ProjectsNavGroup({ onNavigate }: { onNavigate?: () => void }) {
  const attention = useProjectAttention();
  const projects = attention.data ?? [];
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const inSpace = pathname.startsWith("/projects");
  const activeId = (() => {
    const m = pathname.match(/^\/projects\/([^/]+)/);
    return m ? m[1] : null;
  })();

  const [expanded, setExpanded] = useState(
    () => localStorage.getItem(EXPAND_KEY) !== "0",
  );
  const toggle = () => {
    setExpanded((e) => {
      const next = !e;
      localStorage.setItem(EXPAND_KEY, next ? "1" : "0");
      return next;
    });
  };

  return (
    <div className="space-y-0.5">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={expanded}
        className={cn(
          "group relative flex w-full items-center gap-3 rounded-control px-2.5 py-2 text-sm font-medium",
          "text-moon-400 transition-colors duration-100 hover:bg-ink-800 hover:text-moon-100",
          inSpace &&
            "!text-moon-100 bg-ink-800 shadow-[inset_2px_0_0_var(--color-lamp)]",
        )}
      >
        <FolderKanban size={17} className="shrink-0" />
        <span>Projects</span>
        <ChevronRight
          size={14}
          className={cn(
            "ml-auto shrink-0 text-moon-600 transition-transform duration-150",
            expanded && "rotate-90",
          )}
        />
      </button>

      {expanded && projects.length > 0 && (
        <div className="space-y-0.5 pb-1">
          {projects.map((p) => (
            <Link
              key={p.id}
              to="/projects/$id"
              params={{ id: p.id }}
              onClick={onNavigate}
              className={cn(
                "relative flex items-center gap-2.5 rounded-control py-1.5 pr-2 pl-7 text-[13px] font-medium",
                "text-moon-400 transition-colors hover:bg-ink-800 hover:text-moon-100",
                p.id === activeId &&
                  "!text-moon-100 bg-ink-800 shadow-[inset_2px_0_0_var(--color-lamp)]",
              )}
            >
              <span className="relative flex shrink-0 items-center">
                <ProjectDot color={p.color} />
                {p.running > 0 && (
                  <span
                    aria-hidden
                    className="absolute -right-1 -top-1 h-1.5 w-1.5 rounded-full bg-lamp ring-2 ring-ink-900 motion-safe:animate-pulse"
                  />
                )}
              </span>
              <span className="truncate">{p.name}</span>
              {p.needs_you > 0 && (
                <span className="ml-auto shrink-0 rounded-full bg-lamp/15 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-lamp">
                  {p.needs_you}
                </span>
              )}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
