import { useMemo } from "react";
import { Link } from "@tanstack/react-router";
import { ChevronRight, FolderKanban, Plus } from "lucide-react";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { EmptyState } from "@/ui/EmptyState";
import { Tooltip } from "@/ui/Tooltip";
import { ProjectDot } from "@/components/ProjectDot";
import { useProjects } from "@/api/projects";
import { useAnalyticsSpend, isUnpriced, type ProjectRollupRow } from "@/api/analytics";
import { formatUsd } from "@/lib/status";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";

/**
 * /projects — pick a project. The sidebar entry target: a grid of project cards
 * each carrying just-enough 30-day signal (runs, cost, success rate, last
 * changed) to choose one without opening it. Archived projects collapse under a
 * details group at the bottom. The full per-project pulse + management queue
 * lives on /projects/$id (see ProjectPage). All signal here comes from one
 * analytics spend rollup (by_project), not a query per card.
 */
export function ProjectsIndexPage() {
  const activeQ = useProjects();
  const archivedQ = useProjects({ archived: true });
  const spendQ = useAnalyticsSpend("30d");

  const rollup = useMemo(() => {
    const m = new Map<string, ProjectRollupRow>();
    for (const r of spendQ.data?.by_project ?? []) {
      if (r.project_id) m.set(r.project_id, r);
    }
    return m;
  }, [spendQ.data]);

  const active = activeQ.data ?? [];
  const archived = (archivedQ.data ?? []).filter((p) => p.archived_at);

  return (
    <Page
      title="Projects"
      subtitle="Pick a project to manage its tickets, inbox, running work, and recent activity in one place."
      width="wide"
      actions={
        <Button asChild variant="primary">
          <Link to="/settings/$section" params={{ section: "projects" }}>
            <Plus size={15} /> New project
          </Link>
        </Button>
      }
    >
      {activeQ.isLoading ? (
        <div className="grid place-items-center py-20 text-sm text-moon-600">Loading projects…</div>
      ) : active.length === 0 && archived.length === 0 ? (
        <EmptyState
          icon={<FolderKanban size={18} />}
          title="No projects yet"
          description="A project bundles workspace defaults for a repo and groups its tickets, inbox, and runs. Create one in Settings → Projects to give a work area a home."
          action={
            <Button asChild variant="primary">
              <Link to="/settings/$section" params={{ section: "projects" }}>
                <Plus size={15} /> Create a project
              </Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {active.map((p) => (
              <ProjectCard key={p.id} project={p} rollup={rollup.get(p.id)} />
            ))}
          </div>

          {archived.length > 0 && (
            <details className="group mt-8">
              <summary className="flex cursor-pointer select-none items-center gap-2 text-xs font-semibold uppercase tracking-wide text-moon-600 hover:text-moon-400">
                <ChevronRight size={13} className="transition-transform group-open:rotate-90" />
                Archived ({archived.length})
              </summary>
              <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {archived.map((p) => (
                  <ProjectCard key={p.id} project={p} rollup={rollup.get(p.id)} muted />
                ))}
              </div>
            </details>
          )}
        </>
      )}
    </Page>
  );
}

function ProjectCard({
  project,
  rollup,
  muted,
}: {
  project: import("@/api/types").ProjectOut;
  rollup?: ProjectRollupRow;
  muted?: boolean;
}) {
  const runs = rollup?.run_count ?? 0;
  const unpriced = rollup ? isUnpriced({ total_tokens: rollup.total_tokens, cost: rollup.cost }) : false;
  const cost = rollup?.cost ?? 0;

  return (
    <Link
      to="/projects/$id"
      params={{ id: project.id }}
      className={cn(
        "group flex flex-col gap-3 rounded-card border p-4 shadow-[var(--shadow-raised)] transition-colors",
        muted
          ? "border-ink-700/50 bg-ink-900/40 hover:bg-ink-900"
          : "border-ink-700 bg-ink-900 hover:border-ink-700 hover:bg-ink-800",
      )}
    >
      <div className="flex items-start gap-2.5">
        <ProjectDot color={project.color} className="mt-1.5" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className={cn("truncate font-display text-sm font-semibold", muted ? "text-moon-400" : "text-moon-100")}>
              {project.name}
            </h3>
            {muted && (
              <span className="shrink-0 rounded-full border border-ink-700 bg-ink-800 px-1.5 py-0.5 text-[10px] font-medium text-moon-600">
                Archived
              </span>
            )}
          </div>
          <Tooltip content={project.source_path} mono>
            <span className="mt-0.5 block truncate font-mono text-[11px] text-moon-600">{project.source_path}</span>
          </Tooltip>
        </div>
      </div>

      <div className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-moon-600">
        {runs > 0 ? (
          <>
            <span className="font-mono tabular-nums">
              {runs} run{runs === 1 ? "" : "s"}
            </span>
            <span className="font-mono tabular-nums text-moon-400">
              {unpriced && cost === 0 ? "unpriced" : formatUsd(cost)}
            </span>
            {rollup && rollup.success_rate < 1 && (
              <span className="text-failed">{Math.round((1 - rollup.success_rate) * 100)}% failed</span>
            )}
          </>
        ) : (
          <span className="text-moon-600">No runs in 30d</span>
        )}
        <span className="ml-auto">{relativeTime(project.updated_at)}</span>
      </div>
    </Link>
  );
}
