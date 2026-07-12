import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { ArrowLeft, ExternalLink, Pencil, Plus } from "lucide-react";
import { Button } from "@/ui/Button";
import { ErrorState } from "@/ui/ErrorState";
import { Tooltip } from "@/ui/Tooltip";
import { ProjectDot } from "@/components/ProjectDot";
import { TicketPeek } from "@/routes/tickets/TicketPeek";
import { useProject, useProjects } from "@/api/projects";
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
import { ManagementQueue } from "./ManagementQueue";
import { ProjectInbox, ActivityLedger, ModelsProfiles, GitIntegrationsPanel } from "./ProjectPanels";
import { countByStatus, defaultsFragments, projectFilterToken } from "./shared";

const POLL = 5000;

/**
 * /projects/$id — manage everything about one project in one place: a live
 * pulse, an actionable queue, the project's inbox, recent activity, and where
 * its 30-day spend went. A dashboard (not a board clone) that composes existing
 * project-scoped APIs. See docs/design/v2-project-pages.md.
 */
export function ProjectPage() {
  const { id } = useParams({ from: "/app/projects/$id" });
  const navigate = useNavigate();
  const projectQ = useProject(id);
  const projectsQ = useProjects();
  const labelsQ = useLabels();

  const ticketsQ = useTickets({ project_id: id, limit: 500 }, { refetchInterval: POLL });
  const inboxQ = useInbox(id, { refetchInterval: POLL });
  const activityQ = useProjectActivity(id);
  const spendQ = useAnalyticsSpend("30d", id);
  const repoLinksQ = useProjectRepoLinks(id);

  const [peekId, setPeekId] = useState<string | null>(null);

  // Close the peek on Escape (matches the Desk / Tickets peek interaction).
  useKeybinds([
    { combo: "Escape", label: "Close peek", group: "Project", handler: () => setPeekId(null) },
  ]);

  const project = projectQ.data;
  const allTickets = ticketsQ.data ?? [];
  const inboxItems = inboxQ.data ?? [];

  // Resolve the peeked ticket from the already-fetched project sets — instant,
  // no extra fetch. Falls back to undefined (peek closes) if it left the set.
  const peekTicket = useMemo(() => {
    if (!peekId) return undefined;
    const fromList = allTickets.find((t) => t.id === peekId);
    if (fromList) return fromList;
    const fromInbox = inboxItems.find((i) => i.ticket.id === peekId)?.ticket;
    return fromInbox;
  }, [peekId, allTickets, inboxItems]);

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
              <Link to="/projects">Back to projects</Link>
            </Button>
          }
        />
      </div>
    );
  }

  const counts = countByStatus(allTickets);
  const queueTickets = allTickets.filter((t) => t.status !== "inbox" && t.status !== "archived");
  const token = projectFilterToken(project);
  const fragments = defaultsFragments(project);
  const totals = spendQ.data?.totals;
  const inboxCount = inboxItems.length;
  const repoLinks = repoLinksQ.data ?? [];
  const projectMap = new Map((projectsQ.data ?? []).map((p) => [p.id, p]));

  const open = (tid: string) => navigate({ to: "/tickets/$id", params: { id: tid } });

  return (
    <div className={cn("h-full overflow-y-auto", peekTicket && "lg:pr-[420px]")}>
      <div className="mx-auto w-full max-w-[2200px] px-4 py-5 sm:px-6 sm:py-6">
        {/* Header */}
        <div className="mb-5">
          <Link
            to="/projects"
            className="inline-flex items-center gap-1.5 text-xs text-moon-600 hover:text-moon-400"
          >
            <ArrowLeft size={13} /> Projects
          </Link>
          <div className="mt-2 flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
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
                  <ExternalLink size={14} /> Open in board
                </Link>
              </Button>
              <Button asChild variant="ghost" size="sm">
                <Link to="/settings/$section" params={{ section: "projects" }}>
                  <Pencil size={14} /> Edit
                </Link>
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
        </div>

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

/** Tile contents: the number leads, the label is a quiet caption. `accent`
 *  lights up a non-zero Running / Review / Inbox so the eye lands on what
 *  needs attention. */
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
