import type { ProjectOut, RunOut } from "@/api/types";
import { BoardCard } from "./BoardCard";
import type { GroupBy, TicketGroup } from "./displayModel";
import { cn } from "@/lib/cn";

/** Read/organize board for non-status groupings (project / priority / label /
 *  profile). Drag-and-drop is a status-only affordance, so cards here aren't
 *  draggable — the columns follow the chosen grouping. */
export function GroupedBoard({
  groups,
  groupBy,
  projects,
  latestRun,
  onSelect,
  onOpen,
  onRangeSelect,
  onToggleSelect,
  selected,
  focusedId,
  peekedId,
  peekOpen,
}: {
  groups: TicketGroup[];
  groupBy: GroupBy;
  projects: Map<string, ProjectOut>;
  latestRun: Map<string, RunOut>;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
  onRangeSelect: (id: string) => void;
  onToggleSelect: (id: string) => void;
  selected: Set<string>;
  focusedId?: string;
  peekedId?: string;
  /** Side peek open — drop the column min-width floor so they reflow. */
  peekOpen?: boolean;
}) {
  return (
    <div className="flex h-full snap-x snap-mandatory gap-3 overflow-x-auto pb-4 md:snap-none">
      {groups.map((g) => (
        <div
          key={g.key}
          className={cn(
            "flex min-h-0 flex-col",
            "w-[85vw] shrink-0 snap-start md:w-0 md:shrink md:flex-1",
            peekOpen ? "md:min-w-0" : "md:min-w-[240px]",
          )}
        >
          <div className="mb-2 flex items-center gap-2 px-1">
            {g.color && (
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: g.color }} />
            )}
            <span
              className={cn(
                "font-display text-xs font-semibold uppercase tracking-wide",
                g.status === "running" ? "dawn-text" : "text-moon-400",
              )}
            >
              {g.label}
            </span>
            <span className="rounded-full bg-ink-800 px-1.5 py-0.5 font-mono text-[10px] text-moon-600">
              {g.tickets.length}
            </span>
          </div>
          <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto overscroll-contain rounded-card border border-dashed border-ink-700/60 bg-ink-950/30 p-2">
            {g.tickets.map((t) => (
              <BoardCard
                key={t.id}
                ticket={t}
                project={t.project_id ? projects.get(t.project_id) : undefined}
                latestRun={latestRun.get(t.id)}
                onSelect={() => onSelect(t.id)}
                onOpen={() => onOpen(t.id)}
                onRangeSelect={() => onRangeSelect(t.id)}
                onToggleSelect={() => onToggleSelect(t.id)}
                selected={selected.has(t.id)}
                focused={focusedId === t.id}
                peeked={peekedId === t.id}
                hideProject={groupBy === "project"}
              />
            ))}
            {g.tickets.length === 0 && (
              <p className="px-2 py-6 text-center text-xs text-moon-600">Empty</p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
