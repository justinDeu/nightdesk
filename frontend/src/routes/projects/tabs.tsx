import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { ExternalLink } from "lucide-react";
import { cn } from "@/lib/cn";

/** The four project-space horizon tabs (docs/design/project-control-plane.md). */
export type ProjectTab = "overview" | "plan" | "history" | "settings";

export const PROJECT_TABS: { id: ProjectTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "plan", label: "Plan" },
  { id: "history", label: "History" },
  { id: "settings", label: "Settings" },
];

/** Horizon tab nav under the project header: Overview | Plan | History | Settings,
 *  plus a quiet right-aligned "Trends ↗ Analytics" link (to /analytics?project=id). */
export function HorizonTabs({
  projectId,
  tab,
  onTab,
}: {
  projectId: string;
  tab: ProjectTab;
  onTab: (t: ProjectTab) => void;
}) {
  return (
    <div className="flex items-center gap-6 border-b border-ink-700 px-4 sm:px-6">
      <nav role="tablist" aria-label="Project horizon" className="flex items-center gap-6">
        {PROJECT_TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={t.id === tab}
            onClick={() => onTab(t.id)}
            className={cn(
              "border-b-2 px-0 py-3 text-sm font-medium transition-colors",
              t.id === tab
                ? "border-lamp text-moon-100"
                : "border-transparent text-moon-600 hover:text-moon-400",
            )}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <Link
        to="/analytics"
        search={{ project: projectId }}
        className="ml-auto inline-flex items-center gap-1 py-3 text-xs text-moon-600 transition-colors hover:text-moon-400"
      >
        Trends <ExternalLink size={11} /> Analytics
      </Link>
    </div>
  );
}

/** A quiet placeholder for a not-yet-built horizon tab. */
export function ProjectStub({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="mx-auto w-full max-w-[900px] px-4 py-16 sm:px-6">
      <h2 className="font-display text-base font-semibold text-moon-100">{title}</h2>
      {children && <p className="mt-2 max-w-prose text-sm text-moon-400">{children}</p>}
    </div>
  );
}

/** Plan tab — "Coming soon" with one sentence pointing at the design doc. */
export function PlanTab() {
  return (
    <ProjectStub title="Coming soon">
      The roadmap workbench — coverage badges, dependency workstream lanes, and a prose-to-tickets
      breakdown workbench. See{" "}
      <code className="rounded bg-ink-800 px-1 py-0.5 font-mono text-[11px] text-moon-400">
        docs/design/project-control-plane.md
      </code>{" "}
      §Plan.
    </ProjectStub>
  );
}

/** History tab — plain placeholder (a later ticket builds the day-grouped ledger). */
export function HistoryTab() {
  return <ProjectStub title="History" />;
}

/** Settings tab — plain placeholder (a later ticket lifts project settings here). */
export function SettingsTab() {
  return <ProjectStub title="Settings" />;
}
