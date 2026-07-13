import { useMemo, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useQueries } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { qk } from "@/api";
import { integrationsApi, useConnections, useProjectRepoLinks } from "@/api/integrations";
import { useTickets } from "@/api/tickets";
import { useInbox } from "@/api/inbox";
import { useProjectAgents, useAgentAttention } from "@/api/agents";
import { useRun } from "@/api/runs";
import { useAnalyticsSpend } from "@/api/analytics";
import { useNow } from "@/lib/useNow";
import { durationBetween } from "@/lib/time";
import { formatUsd } from "@/lib/status";
import { ExternalItemPeek } from "@/components/ExternalItemPeek";
import { PriorityChip } from "@/components/PriorityChip";
import { cn } from "@/lib/cn";
import type { ProjectOut, RepoLinkOut, TicketOut } from "@/api/types";
import { countByStatus, defaultsFragments } from "./shared";
import { VerdictSection } from "./overview/VerdictSection";
import { WaitingSection } from "./overview/WaitingSection";
import { InboxSection } from "./overview/InboxSection";
import { ANCHORS, jumpTo } from "./overview/overview";

const POLL = 5000;

/**
 * Overview horizon tab — a single full-width ledger column: a text signal strip,
 * RUNNING rows, NEEDS YOUR VERDICT, WAITING ON YOUR REPLY, INBOX, and a quiet
 * footer. No card grids, no right rail; every action acts in place (the only
 * exits are the ticket page and the Trends→Analytics link in the header).
 *
 * Replaces the v2 pulse-tiles + management-queue + right-rail panels. See
 * docs/design/project-control-plane.md §Overview.
 */
export function OverviewTab({
  project,
  onEdit,
}: {
  project: ProjectOut;
  onEdit: () => void;
}) {
  const navigate = useNavigate();

  const ticketsQ = useTickets(
    { project_id: project.id, limit: 500 },
    { refetchInterval: POLL },
  );
  const inboxQ = useInbox(project.id, { refetchInterval: POLL });
  const spendQ = useAnalyticsSpend("30d", project.id);
  const repoLinksQ = useProjectRepoLinks(project.id);
  const connectionsQ = useConnections();
  const projectAgentsQ = useProjectAgents(project.id, { refetchInterval: POLL });
  const attentionQ = useAgentAttention(POLL);

  const allTickets = ticketsQ.data ?? [];
  const inboxItems = inboxQ.data ?? [];
  const repoLinks = repoLinksQ.data ?? [];

  const counts = countByStatus(allTickets);
  const running = allTickets.filter((t) => t.status === "running");
  const review = allTickets.filter((t) => t.status === "review");

  // Agent pending questions scoped to THIS project (sessions carry project_id).
  const projectAgentIds = useMemo(
    () => new Set((projectAgentsQ.data ?? []).map((a) => a.id)),
    [projectAgentsQ.data],
  );
  const projectPending = useMemo(
    () => (attentionQ.data?.pending ?? []).filter((p) => projectAgentIds.has(p.session_id)),
    [attentionQ.data, projectAgentIds],
  );

  // MRs awaiting the connection user's review, fanned out across the project's
  // repo links. One source of truth: the signal-strip count and the waiting
  // feed both read this.
  const mrQueries = useQueries({
    queries: repoLinks.map((repo) => ({
      queryKey: qk.integrations.mrs(repo.id, { state: "opened" }),
      queryFn: () => integrationsApi.listMrs(repo.id, { state: "opened" }),
      staleTime: 30_000,
      retry: false,
    })),
  });
  const awaitingMrs = useMemo(() => {
    const out: { repo: RepoLinkOut; iid: string; title: string }[] = [];
    repoLinks.forEach((repo, i) => {
      for (const m of mrQueries[i].data?.items ?? []) {
        if (m.awaiting_your_review) {
          out.push({ repo, iid: String(m.iid), title: m.title });
        }
      }
    });
    return out;
  }, [repoLinks, mrQueries]);

  const repoHealth = useRepoHealth(repoLinks, connectionsQ.data ?? []);

  const [mrPeek, setMrPeek] = useState<{ repo: RepoLinkOut; iid: string } | null>(null);
  const openTicket = (id: string) => navigate({ to: "/tickets/$id", params: { id } });

  const spend = spendQ.data?.totals;
  const hasContent =
    running.length > 0 ||
    review.length > 0 ||
    inboxItems.length > 0 ||
    projectPending.length > 0 ||
    awaitingMrs.length > 0;

  return (
    <div className={cn("h-full overflow-y-auto", mrPeek && "lg:pr-[460px]")}>
      <div className="w-full px-5 py-4 sm:px-6 sm:py-5">
        <SignalStrip
          counts={counts}
          inbox={inboxItems.length}
          awaitingMrs={awaitingMrs.length}
          agentsWaiting={projectPending.length}
          repoHealth={repoHealth}
          spend={spend?.cost ?? 0}
        />

        {/* RUNNING */}
        {running.length > 0 && (
          <RunningSection tickets={running} onOpen={openTicket} />
        )}

        <VerdictSection tickets={review} />

        <WaitingSection
          pending={projectPending}
          awaitingMrs={awaitingMrs}
          onOpenMr={(repo, iid) => setMrPeek({ repo, iid })}
        />

        <InboxSection items={inboxItems} />

        {!hasContent && <EmptyOverview onEdit={onEdit} />}

        <Footer
          counts={counts}
          inbox={inboxItems.length}
          repoLinks={repoLinks}
          repoHealth={repoHealth}
          project={project}
          onEdit={onEdit}
        />
      </div>

      {mrPeek && (
        <ExternalItemPeek
          key={`${mrPeek.repo.id}:${mrPeek.iid}`}
          repo={mrPeek.repo}
          kind="merge_request"
          iid={mrPeek.iid}
          projectId={project.id}
          onClose={() => setMrPeek(null)}
        />
      )}
    </div>
  );
}

// --- signal strip --------------------------------------------------------------

function SignalStrip({
  counts,
  inbox,
  awaitingMrs,
  agentsWaiting,
  repoHealth,
  spend,
}: {
  counts: { running: number; review: number; queued: number; draft: number };
  inbox: number;
  awaitingMrs: number;
  agentsWaiting: number;
  repoHealth: { label: string; tone: "ok" | "warn" | "muted" } | null;
  spend: number;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-center gap-x-0 gap-y-1 rounded-card border border-ink-800 bg-ink-900 px-3.5 py-2.5 text-[12.5px] text-moon-600">
      <SigItem tone={counts.running > 0 ? "running" : undefined} bold={counts.running > 0} anchor={ANCHORS.running}>
        {counts.running} running
      </SigItem>
      <SigItem tone={counts.review > 0 ? "review" : undefined} bold={counts.review > 0} anchor={ANCHORS.verdict}>
        {counts.review} review
      </SigItem>
      <SigItem anchor={ANCHORS.footer}>{counts.queued} queued</SigItem>
      <SigItem anchor={ANCHORS.footer}>{counts.draft} draft</SigItem>
      <SigItem tone={inbox > 0 ? "warn" : undefined} bold={inbox > 0} anchor={ANCHORS.inbox}>
        {inbox} inbox
      </SigItem>
      {awaitingMrs > 0 && (
        <SigItem tone="warn" bold anchor={ANCHORS.waiting}>
          {awaitingMrs} MR{awaitingMrs === 1 ? "" : "s"} awaiting review
        </SigItem>
      )}
      {agentsWaiting > 0 && (
        <SigItem tone="warn" bold anchor={ANCHORS.waiting}>
          {agentsWaiting} agent{agentsWaiting === 1 ? "" : "s"} waiting
        </SigItem>
      )}
      {repoHealth && (
        <SigItem tone={repoHealth.tone === "ok" ? "ok" : repoHealth.tone === "warn" ? "warn" : undefined}>
          {repoHealth.label}
        </SigItem>
      )}
      <SigItem>30d {formatUsd(spend)}</SigItem>
    </div>
  );
}

function SigItem({
  children,
  tone,
  bold,
  anchor,
}: {
  children: React.ReactNode;
  tone?: "running" | "review" | "warn" | "ok";
  bold?: boolean;
  anchor?: string;
}) {
  const cls = cn(
    "px-2.5 first:pl-0 [&:not(:last-child)]:border-r [&:not(:last-child)]:border-ink-800",
    tone === "running" && "text-dawn",
    tone === "review" && "text-review",
    tone === "warn" && "text-warn",
    tone === "ok" && "text-success",
    bold && "font-semibold",
  );
  if (!anchor) return <span className={cls}>{children}</span>;
  return (
    <button onClick={() => jumpTo(anchor)} className={cn(cls, "hover:underline")}>
      {children}
    </button>
  );
}

// --- RUNNING -------------------------------------------------------------------

/** The RUNNING ledger. Owns ONE ticking clock (shared across every running row)
 *  so a busy project doesn't mount N independent 1s intervals. */
function RunningSection({
  tickets,
  onOpen,
}: {
  tickets: TicketOut[];
  onOpen: (id: string) => void;
}) {
  const now = useNow(tickets.length > 0, 1000);
  return (
    <section id={ANCHORS.running} className="mb-7 scroll-mt-24">
      <div className="mb-1.5 flex items-baseline gap-2 border-b border-ink-800 pb-1.5">
        <span className="font-display text-[11.5px] font-bold uppercase tracking-[0.06em] text-moon-400">
          Running
        </span>
        <span className="text-[11px] text-moon-600">({tickets.length})</span>
        <span className="ml-auto text-[10.5px] italic text-moon-600">
          → ticket page for inspect / steer
        </span>
      </div>
      <div>
        {tickets.map((t) => (
          <RunningRow key={t.id} ticket={t} now={now} onOpen={onOpen} />
        ))}
      </div>
    </section>
  );
}

function RunningRow({
  ticket,
  now,
  onOpen,
}: {
  ticket: TicketOut;
  now: number;
  onOpen: (id: string) => void;
}) {
  // Live elapsed + cost from the ticket's current run.
  const runQ = useRun(ticket.current_run_id ?? undefined);
  const run = runQ.data;
  return (
    <button
      onClick={() => onOpen(ticket.id)}
      className="flex w-full items-center gap-2.5 border-b border-ink-900 px-2.5 py-2 text-left last:border-b-0 hover:bg-ink-900/50"
    >
      <span className="relative flex shrink-0 items-center">
        <span className="h-2 w-2 animate-pulse rounded-full bg-lamp shadow-[0_0_0_3px_rgba(69,192,132,0.16)]" />
      </span>
      <PriorityChip value={ticket.priority} hideNone />
      <span className="min-w-0 flex-1 truncate text-[12.5px] text-moon-100">{ticket.title}</span>
      <span className="flex shrink-0 items-center gap-2.5 font-mono text-[11px] text-moon-600">
        <span>{(run?.started_at && durationBetween(run.started_at, null, now)) || "—"}</span>
        <span>{formatUsd(run?.cost_usd)}</span>
      </span>
    </button>
  );
}

// --- footer --------------------------------------------------------------------

function Footer({
  counts,
  inbox,
  repoLinks,
  repoHealth,
  project,
  onEdit,
}: {
  counts: { queued: number; draft: number };
  inbox: number;
  repoLinks: RepoLinkOut[];
  repoHealth: { label: string } | null;
  project: ProjectOut;
  onEdit: () => void;
}) {
  const repo = repoLinks[0];
  const defaults = defaultsFragments(project)
    .map((f) => f.label)
    .slice(0, 2);
  return (
    <footer
      id={ANCHORS.footer}
      className="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1 border-t border-ink-800 pt-3 font-mono text-[11.5px] text-moon-600"
    >
      <span>
        queued {counts.queued} · draft {counts.draft} · inbox {inbox}
      </span>
      {repo && (
        <span>
          · repo{" "}
          <span className="text-moon-400">{repo.display_name || repo.external_path}</span>{" "}
          {repoHealth?.label.replace(/^repo\s/i, "")}
        </span>
      )}
      {defaults.length > 0 && <span>· {defaults.join(" · ")}</span>}
      <span>·</span>
      <button onClick={onEdit} className="inline-flex items-center gap-1 text-moon-400 hover:text-moon-200">
        <Pencil size={11} /> Edit
      </button>
    </footer>
  );
}

function EmptyOverview({ onEdit }: { onEdit: () => void }) {
  return (
    <div className="rounded-card border border-dashed border-ink-700 bg-ink-900/40 px-6 py-12 text-center">
      <h3 className="font-display text-sm font-semibold text-moon-100">This project is quiet</h3>
      <p className="mx-auto mt-1 max-w-sm text-sm text-moon-400">
        Nothing running, in review, waiting on you, or in the inbox. Capture a new ticket, or review
        project defaults.
      </p>
      <div className="mt-3 flex justify-center gap-2">
        <button
          onClick={onEdit}
          className="rounded-control border border-ink-700 bg-ink-800 px-3 py-1.5 text-[12px] text-moon-100 hover:bg-ink-700"
        >
          Project settings
        </button>
      </div>
    </div>
  );
}

// --- repo health (connection status → tone) ------------------------------------

function useRepoHealth(
  repoLinks: RepoLinkOut[],
  connections: { id: string; status: string }[],
): { label: string; tone: "ok" | "warn" } | null {
  return useMemo(() => {
    if (repoLinks.length === 0) return null;
    const statusOf = (cid: string) => connections.find((c) => c.id === cid)?.status;
    // A dead connection (bad token / unreachable) is the only "not ok" state we
    // surface; anything else (ok / unchecked / transient) reads as healthy here.
    const unreachable = repoLinks.some((rl) => {
      const s = statusOf(rl.connection_id);
      return s === "auth_failed" || s === "unreachable";
    });
    return unreachable
      ? { label: "repo unreachable", tone: "warn" }
      : { label: "repo ok", tone: "ok" };
  }, [repoLinks, connections]);
}
