import { useMemo, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { Plus } from "lucide-react";
import { Button } from "@/ui/Button";
import { useProjects } from "@/api/projects";
import { useTickets } from "@/api/tickets";
import { useInbox } from "@/api/inbox";
import { useProjectActivity } from "@/api/projectActivity";
import { useAnalyticsSpend } from "@/api/analytics";
import { useLabels } from "@/api/labels";
import { useProjectRepoLinks } from "@/api/integrations";
import { useKeybinds } from "@/lib/keymap";
import { formatUsd } from "@/lib/status";
import { openComposer } from "@/components/composerBus";
import { cn } from "@/lib/cn";
import type { ProjectOut } from "@/api/types";
import { ManagementQueue } from "./ManagementQueue";
import { ProjectInbox, ActivityLedger, ModelsProfiles, GitIntegrationsPanel } from "./ProjectPanels";
import { countByStatus, projectFilterToken } from "./shared";
import { TicketPeek } from "@/routes/tickets/TicketPeek";

const POLL = 5000;

/**
 * Overview horizon tab — the existing project-page content (pulse tiles +
 * actionable queue + right-rail panels), carried forward unchanged. A later
 * ticket replaces this with the unified-ledger Overview from the design doc
 * (docs/design/project-control-plane.md §Overview). Renders inside the project
 * space below the shared header + horizon tabs.
 */
export function OverviewTab({ project }: { project: ProjectOut }) {
  const navigate = useNavigate();
  const projectsQ = useProjects();
  const labelsQ = useLabels();

  const ticketsQ = useTickets({ project_id: project.id, limit: 500 }, { refetchInterval: POLL });
  const inboxQ = useInbox(project.id, { refetchInterval: POLL });
  const activityQ = useProjectActivity(project.id);
  const spendQ = useAnalyticsSpend("30d", project.id);
  const repoLinksQ = useProjectRepoLinks(project.id);

  const [peekId, setPeekId] = useState<string | null>(null);

  useKeybinds([
    { combo: "Escape", label: "Close peek", group: "Project", handler: () => setPeekId(null) },
  ]);

  const allTickets = ticketsQ.data ?? [];
  const inboxItems = inboxQ.data ?? [];

  const peekTicket = useMemo(() => {
    if (!peekId) return undefined;
    const fromList = allTickets.find((t) => t.id === peekId);
    if (fromList) return fromList;
    const fromInbox = inboxItems.find((i) => i.ticket.id === peekId)?.ticket;
    return fromInbox;
  }, [peekId, allTickets, inboxItems]);

  const counts = countByStatus(allTickets);
  const queueTickets = allTickets.filter((t) => t.status !== "inbox" && t.status !== "archived");
  const token = projectFilterToken(project);
  const totals = spendQ.data?.totals;
  const inboxCount = inboxItems.length;
  const repoLinks = repoLinksQ.data ?? [];
  const projectMap = new Map((projectsQ.data ?? []).map((p) => [p.id, p]));

  const open = (tid: string) => navigate({ to: "/tickets/$id", params: { id: tid } });

  return (
    <div className={cn("h-full overflow-y-auto", peekTicket && "lg:pr-[420px]")}>
      <div className="mx-auto w-full max-w-[2200px] px-4 py-5 sm:px-6 sm:py-6">
        {/* Pulse tiles */}
        <div className="mb-5 grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-7">
          <Link to="/tickets" search={{ f: `status:running ${token}` }} className={tileCls}>
            <TileInner label="Running" value={counts.running} accent={counts.running > 0} />
          </Link>
          <Link to="/tickets" search={{ f: `status:review ${token}` }} className={tileCls}>
            <TileInner label="Review" value={counts.review} accent={counts.review > 0} />
          </Link>
          <Link to="/tickets" search={{ f: `status:queued ${token}` }} className={tileCls}>
            <TileInner label="Queued" value={counts.queued} />
          </Link>
          <Link to="/tickets" search={{ f: `status:draft ${token}` }} className={tileCls}>
            <TileInner label="Draft" value={counts.draft} />
          </Link>
          <Link to="/inbox" className={tileCls}>
            <TileInner label="Inbox" value={inboxCount} accent={inboxCount > 0} />
          </Link>
          <Link to="/analytics" search={{ project: project.id }} className={tileCls}>
            <TileInner label="Spend 30d" value={formatUsd(totals?.cost ?? 0)} />
          </Link>
          <Link to="/analytics" search={{ project: project.id }} className={tileCls}>
            <TileInner label="Runs 30d" value={totals?.run_count ?? 0} />
          </Link>
        </div>

        {/* Main grid: queue (left) + stacked panels (right) */}
        <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
          <div className="min-w-0">
            {queueTickets.length > 0 ? (
              <ManagementQueue
                tickets={queueTickets}
                boardFilter={token}
                peekedId={peekId ?? undefined}
                onPeek={setPeekId}
                onOpen={open}
              />
            ) : (
              <div className="rounded-card border border-dashed border-ink-700 bg-ink-900/40 px-6 py-10 text-center">
                <h3 className="font-display text-sm font-semibold text-moon-100">
                  No actionable tickets
                </h3>
                <p className="mx-auto mt-1 max-w-sm text-sm text-moon-400">
                  Nothing in review, running, queued, or draft for this project. Capture a new
                  ticket, or open the board to see everything.
                </p>
                <div className="mt-3 flex justify-center gap-2">
                  <Button
                    variant="primary"
                    size="sm"
                    leadingIcon={<Plus size={14} />}
                    onClick={() => openComposer({ project_id: project.id })}
                  >
                    New ticket
                  </Button>
                  <Button asChild variant="ghost" size="sm">
                    <Link to="/tickets" search={{ f: token }}>
                      Open board
                    </Link>
                  </Button>
                </div>
              </div>
            )}
          </div>

          <div className="flex min-w-0 flex-col gap-4">
            {repoLinks.length > 0 && (
              <GitIntegrationsPanel projectId={project.id} repos={repoLinks} filterToken={token} />
            )}
            <ProjectInbox items={inboxItems} />
            <ActivityLedger rows={activityQ.data ?? []} />
            <ModelsProfiles
              byModel={spendQ.data?.by_model ?? []}
              byProfile={spendQ.data?.by_profile ?? []}
              totalRuns={totals?.run_count ?? 0}
              totalCost={totals?.cost ?? 0}
            />
          </div>
        </div>
      </div>

      {peekTicket && (
        <TicketPeek
          key={peekTicket.id}
          ticket={peekTicket}
          project={peekTicket.project_id ? projectMap.get(peekTicket.project_id) : undefined}
          projects={projectsQ.data ?? []}
          labels={labelsQ.data ?? []}
          onClose={() => setPeekId(null)}
          onOpenFull={() => open(peekTicket.id)}
        />
      )}
    </div>
  );
}

/** Shared surface + elevation classes for a pulse tile. */
const tileCls =
  "flex flex-col gap-1 rounded-card border border-ink-700 bg-ink-900 px-3 py-2.5 shadow-[var(--shadow-raised)] transition-colors hover:bg-ink-800";

function TileInner({
  label,
  value,
  accent,
}: {
  label: string;
  value: number | string;
  accent?: boolean;
}) {
  return (
    <>
      <span
        className={cn(
          "font-display text-2xl font-semibold leading-none tabular-nums",
          accent ? "text-lamp" : "text-moon-100",
        )}
      >
        {value}
      </span>
      <span className="text-[11px] font-medium uppercase tracking-wide text-moon-600">{label}</span>
    </>
  );
}
