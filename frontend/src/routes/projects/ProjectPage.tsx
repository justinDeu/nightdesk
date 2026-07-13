import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { Pencil, Plus } from "lucide-react";
import { Button } from "@/ui/Button";
import { ErrorState } from "@/ui/ErrorState";
import { Tooltip } from "@/ui/Tooltip";
import { ProjectDot } from "@/components/ProjectDot";
import { useProject, useProjectAttention } from "@/api/projects";
import { useAnalyticsSpend } from "@/api/analytics";
import { useKeybinds } from "@/lib/keymap";
import { formatUsd } from "@/lib/status";
import { openComposer } from "@/components/composerBus";
import { cn } from "@/lib/cn";
import { OverviewTab } from "./OverviewTab";
import { ProjectStrip } from "./ProjectStrip";
import { HorizonTabs, PlanTab, HistoryTab, SettingsTab, type ProjectTab } from "./tabs";
import { defaultsFragments, projectFilterToken } from "./shared";

/**
 * /projects/$id — the project control plane (docs/design/project-control-plane.md
 * §Chrome + §Frame). Chrome: project strip (hop), header (identity + stat row +
 * actions), horizon tabs (Overview | Plan | History | Settings + Trends link).
 * Overview renders the existing project-page content for now; Plan/History/
 * Settings are stubs filled by later tickets.
 *
 * `]`/`[` cycle projects in strip order (route-scoped, so `[` shadows the global
 * sidebar-toggle only inside the project space). The active horizon tab
 * persists across hops (it lives in the URL search params).
 */
export function ProjectPage() {
  const { id } = useParams({ from: "/app/projects/$id" });
  const { tab } = useSearch({ from: "/app/projects/$id" });
  const activeTab: ProjectTab = tab ?? "overview";
  const navigate = useNavigate();
  const projectQ = useProject(id);
  const attention = useProjectAttention();
  const spendQ = useAnalyticsSpend("30d", id);

  const project = projectQ.data;
  const order = attention.data ?? [];

  const goTab = (t: ProjectTab) =>
    navigate({ to: "/projects/$id", params: { id }, search: { tab: t } });

  // `]` / `[ cycle projects in strip order, carrying the active tab along.
  const cycle = (dir: 1 | -1) => {
    if (order.length < 2) return;
    const idx = order.findIndex((p) => p.id === id);
    if (idx < 0) return;
    const next = order[(idx + dir + order.length) % order.length];
    navigate({ to: "/projects/$id", params: { id: next.id }, search: { tab: activeTab } });
  };
  useKeybinds([
    { combo: "]", label: "Next project", group: "Project", scope: "route", handler: () => cycle(1) },
    { combo: "[", label: "Previous project", group: "Project", scope: "route", handler: () => cycle(-1) },
  ]);

  if (projectQ.isLoading) {
    return (
      <div className="grid h-full place-items-center text-sm text-moon-600">Loading project…</div>
    );
  }
  if (!project) {
    return (
      <div className="grid h-full place-items-center px-4">
        <ErrorState
          className="w-full max-w-md"
          title="Project not found"
          description="This project may have been deleted, or the link is out of date."
          action={
            <Button asChild variant="primary">
              <Link to="/">Back to desk</Link>
            </Button>
          }
        />
      </div>
    );
  }

  const fragments = defaultsFragments(project);
  const token = projectFilterToken(project);
  // success_rate lives on the per-project rollup row, not on the spend totals
  // bucket; fall back to totals (cost/runs) when the rollup is absent.
  const spend = spendQ.data;
  const rollup = spend?.by_project?.find((r) => r.project_id === id);
  const runs = rollup?.run_count ?? spend?.totals.run_count ?? 0;
  const cost = rollup?.cost ?? spend?.totals.cost ?? 0;
  const failRate =
    rollup && rollup.run_count > 0 ? Math.round((1 - rollup.success_rate) * 100) : 0;

  return (
    <div className="flex h-full flex-col">
      <ProjectStrip currentId={id} tab={activeTab} />

      {/* Project header: identity + quiet stat row + actions */}
      <div className="border-b border-ink-700 px-4 pt-4 sm:px-6">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2.5">
              <ProjectDot color={project.color} className="h-3 w-3" />
              <h1 className="font-display text-xl font-semibold tracking-tight text-moon-100">
                {project.name}
              </h1>
              {project.archived_at && (
                <span className="rounded-full border border-ink-700 bg-ink-800 px-1.5 py-0.5 text-[10px] font-medium text-moon-600">
                  Archived
                </span>
              )}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-moon-600">
              <Tooltip content={project.source_path} mono>
                <span className="font-mono text-moon-500">{project.source_path}</span>
              </Tooltip>
              {project.default_toolchains.length > 0 && (
                <span className="text-moon-600">
                  · {project.default_toolchains.length} toolset
                  {project.default_toolchains.length === 1 ? "" : "s"}
                </span>
              )}
              {runs > 0 && (
                <>
                  <span className="text-moon-600">· {formatUsd(cost)} 30d</span>
                  <span className="text-moon-600">· {runs} runs</span>
                  {failRate > 0 && <span className="text-failed">· {failRate}% failed</span>}
                </>
              )}
              {fragments.map((f) => (
                <Tooltip key={f.label} content={f.tooltip}>
                  <span className="text-moon-600">· {f.label}</span>
                </Tooltip>
              ))}
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <Button asChild variant="subtle" size="sm">
              <Link to="/tickets" search={{ f: token }}>
                Open in board
              </Link>
            </Button>
            <Button variant="ghost" size="sm" leadingIcon={<Pencil size={14} />} onClick={() => goTab("settings")}>
              Edit
            </Button>
            <Button
              variant="primary"
              size="sm"
              leadingIcon={<Plus size={14} />}
              onClick={() => openComposer({ project_id: project.id })}
            >
              New ticket
            </Button>
          </div>
        </div>

        <HorizonTabs projectId={id} tab={activeTab} onTab={goTab} />
      </div>

      {/* Active horizon tab */}
      <div className={cn("min-h-0 flex-1", activeTab !== "overview" && "overflow-y-auto")}>
        {activeTab === "overview" && <OverviewTab project={project} onEdit={() => goTab("settings")} />}
        {activeTab === "plan" && <PlanTab />}
        {activeTab === "history" && <HistoryTab project={project} />}
        {activeTab === "settings" && <SettingsTab />}
      </div>
    </div>
  );
}
