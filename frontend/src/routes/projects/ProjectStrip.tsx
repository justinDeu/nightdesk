import { Link } from "@tanstack/react-router";
import { ProjectDot } from "@/components/ProjectDot";
import { useProjectAttention } from "@/api/projects";
import { cn } from "@/lib/cn";
import type { ProjectTab } from "./tabs";

/**
 * The project strip: one compact tab per active project (color dot, name,
 * needs-you badge, lamp pulse when running), rendered across the top of every
 * /projects/$id page. Click hops; ]/[ cycle in the same order (handled in the
 * space wrapper). The strip preserves the active horizon tab when hopping.
 *
 * Order matches the sidebar group (attention desc, then running, then last
 * activity) — the rollup is already display-ordered.
 */
export function ProjectStrip({
  currentId,
  tab,
}: {
  currentId: string;
  tab: ProjectTab;
}) {
  const attention = useProjectAttention();
  const projects = attention.data ?? [];

  return (
    <div className="flex items-center gap-1 overflow-x-auto border-b border-ink-700 px-4 sm:px-6">
      {projects.map((p) => {
        const active = p.id === currentId;
        return (
          <Link
            key={p.id}
            to="/projects/$id"
            params={{ id: p.id }}
            search={{ tab }}
            className={cn(
              "flex shrink-0 items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
              active
                ? "bg-ink-800 text-moon-100"
                : "text-moon-600 hover:bg-ink-800/60 hover:text-moon-400",
            )}
          >
            <span className="relative flex shrink-0 items-center">
              <ProjectDot color={p.color} />
              {p.running > 0 && (
                <span
                  aria-hidden
                  className="absolute -right-1 -top-1 h-1.5 w-1.5 rounded-full bg-lamp ring-2 ring-ink-950 motion-safe:animate-pulse"
                />
              )}
            </span>
            <span className="whitespace-nowrap">{p.name}</span>
            {p.needs_you > 0 && (
              <span className="shrink-0 rounded-full bg-lamp/15 px-1.5 font-mono text-[10px] font-semibold text-lamp">
                {p.needs_you}
              </span>
            )}
          </Link>
        );
      })}
      {projects.length > 1 && (
        <span className="ml-auto hidden shrink-0 pl-3 font-mono text-[11px] text-moon-600 sm:inline">
          <kbd className="rounded border border-ink-700 bg-ink-800 px-1">[</kbd> prev · next{" "}
          <kbd className="rounded border border-ink-700 bg-ink-800 px-1">]</kbd>
        </span>
      )}
    </div>
  );
}
