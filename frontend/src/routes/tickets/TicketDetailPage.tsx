import { useEffect, useMemo } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { History, Maximize2, Zap } from "lucide-react";
import { Button } from "@/ui/Button";
import { ErrorState } from "@/ui/ErrorState";
import { useTicketActions } from "@/lib/ticketActions";
import { qk } from "@/api";
import { useTicket, useUpdateTicket } from "@/api/tickets";
import { useRuns } from "@/api/runs";
import { useProfiles } from "@/api/profiles";
import type { RunOut, TicketOut } from "@/api/types";
import {
  useLiveTranscript,
  buildTodoList,
  buildSubagentList,
  type SubagentSummary,
  type TodoItem,
} from "@/lib/transcript";
import { TranscriptScroller } from "@/components/TranscriptScroller";
import { TasksPanel } from "@/components/TasksPanel";
import { SubagentsPanel } from "@/components/SubagentsPanel";
import { DetailHeader } from "./detail/DetailHeader";
import { PropertiesRail } from "./detail/PropertiesRail";
import { ActivityComposer } from "./detail/ActivityComposer";
import { DescriptionPanel, PromptPanel } from "./detail/BriefPanels";
import { RunTimeline } from "./RunTimeline";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";

/**
 * Full-bleed, state-adaptive ticket workspace.
 *
 * Layout thesis: the center stage adapts to the ticket's life stage. While
 * drafting (no runs yet) the brief — description + agent prompt — is the hero of
 * the center. Once a run exists the brief steps aside into the left metadata
 * rail (collapsed) and the center becomes a single-scroll transcript document
 * that fills the viewport; the activity composer anchors its bottom. Header and
 * every rail stay fixed, so the transcript pane is the only thing that scrolls
 * vertically at desktop widths.
 */
export function TicketDetailPage() {
  const { id } = useParams({ from: "/app/tickets/$id" });
  const qc = useQueryClient();

  const ticketQ = useTicket(id, {
    refetchInterval: (q) => (q.state.data?.status === "running" ? 3000 : false),
  });
  const t = ticketQ.data;
  const runsQ = useRuns(id, { refetchInterval: t?.status === "running" ? 3000 : false });
  const profiles = useProfiles();
  const update = useUpdateTicket(id);

  // One transcript subscription for the whole page — shared by the center
  // transcript and the Tasks / Sub-agents rail panels.
  const hasRunsForTx = (runsQ.data?.length ?? 0) > 0;
  const tx = useLiveTranscript(`/api/v1/tickets/${id}/transcript`, hasRunsForTx);
  const todos = useMemo(() => buildTodoList(tx.events), [tx.events]);
  const subagents = useMemo(() => buildSubagentList(tx.events), [tx.events]);

  // A steer_delivered breadcrumb means a queued follow-up just left the queue;
  // refresh the steer query so the chip clears (its state moved to delivered).
  const steerDeliveredCount = useMemo(
    () => tx.events.filter((e) => e.type === "steer_delivered").length,
    [tx.events],
  );
  useEffect(() => {
    if (steerDeliveredCount > 0) qc.invalidateQueries({ queryKey: qk.tickets.steer(id) });
  }, [steerDeliveredCount, id, qc]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.tickets.detail(id) });
    qc.invalidateQueries({ queryKey: qk.tickets.all });
    qc.invalidateQueries({ queryKey: qk.runs.list(id) });
  };

  if (ticketQ.isLoading) {
    return <div className="p-8 text-sm text-moon-600">Loading ticket…</div>;
  }
  if (ticketQ.isError || !t) {
    return (
      <div className="grid h-full place-items-center px-4">
        <ErrorState
          className="w-full max-w-md"
          title="Ticket not found"
          description="This ticket may have been deleted, or the link is wrong. Check the board for the current set."
          action={
            <Button asChild variant="primary">
              <Link to="/tickets">Back to tickets</Link>
            </Button>
          }
        />
      </div>
    );
  }

  const running = t.status === "running";
  const authoring = t.status === "draft" || t.status === "queued";
  const runs = runsQ.data ?? [];
  const latestRun: RunOut | undefined = runs[0];
  const hasRuns = runs.length > 0;

  const runsPanel = (
    <RunsPanel ticketId={t.id} runs={runs} todos={todos} subagents={subagents} />
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <DetailHeader
        ticket={t}
        latestRun={latestRun}
        onSaveTitle={(title) => update.mutate({ title })}
      />

      <div
        className={cn(
          "grid min-h-0 flex-1 grid-cols-1 overflow-y-auto",
          // Desktop is a fixed three-region document (props / stage / evidence);
          // each region scrolls inside its own bounded column, so the page itself
          // never scrolls. Below lg the grid stacks and scrolls as one column.
          hasRuns
            ? "lg:grid-cols-[264px_minmax(0,1fr)] lg:overflow-hidden xl:grid-cols-[264px_minmax(0,1fr)_minmax(380px,440px)]"
            : "lg:grid-cols-[264px_minmax(0,1fr)] lg:overflow-hidden",
        )}
      >
        {/* LEFT — properties rail (always) + brief + runs evidence when narrow. */}
        <div className="border-b border-ink-700 px-5 py-4 lg:border-b-0 lg:border-r lg:overflow-y-auto">
          <PropertiesRail
            ticket={t}
            brief={
              hasRuns ? (
                <>
                  <DescriptionPanel
                    ticket={t}
                    onSave={(description) => update.mutate({ description })}
                  />
                  <PromptPanel ticket={t} onSave={(prompt) => update.mutate({ prompt })} />
                </>
              ) : null
            }
          />
          {/* Below xl there is no evidence column, so the runs panel folds into
              the rail instead of forcing a second scroll under the transcript. */}
          {hasRuns && (
            <div className="mt-4 border-t border-ink-700/60 pt-4 xl:hidden">{runsPanel}</div>
          )}
        </div>

        {/* CENTER — the adaptive stage. The transcript pane is the only vertical
            scroll on this page at desktop widths: it flexes to fill the column
            and scrolls internally (TranscriptScroller owns the pin/follow). */}
        <section className="flex min-h-0 flex-col lg:overflow-hidden">
          {hasRuns ? (
            <>
              <div className="flex min-h-0 flex-1 flex-col px-5 pt-3">
                <div className="mb-1.5 flex items-center justify-between">
                  <h2 className="font-display text-xs font-semibold uppercase tracking-wide text-moon-400">
                    {running ? "Live transcript" : "Run transcript"}
                  </h2>
                  {latestRun && (
                    <Link
                      to="/tickets/$id/runs/$rid"
                      params={{ id: t.id, rid: latestRun.id }}
                      className="inline-flex items-center gap-1 text-[11px] text-moon-400 hover:text-moon-100"
                    >
                      <Maximize2 size={11} /> Theater
                    </Link>
                  )}
                </div>
                <TranscriptScroller
                  events={tx.events}
                  status={tx.status}
                  running={running}
                  caption={
                    latestRun
                      ? `${latestRun.model_used ?? "run"} · ${relativeTime(latestRun.started_at)}`
                      : undefined
                  }
                  // flex-1 fills the column at desktop (the single scroller);
                  // the mobile max-height keeps a long transcript from expanding
                  // unbounded in the stacked/scrolling layout below lg.
                  className="min-h-0 flex-1 max-h-[60vh] lg:max-h-none"
                />
              </div>
              <ActivityComposer
                ticket={t}
                profiles={profiles.data ?? []}
                running={running}
                onChange={invalidate}
              />
            </>
          ) : (
            // Drafting: the brief is the hero. No transcript yet, so there is no
            // nested-scroll concern — this column is a single authoring surface.
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
              <DescriptionPanel
                ticket={t}
                defaultOpen
                onSave={(description) => update.mutate({ description })}
              />
              <PromptPanel
                ticket={t}
                defaultOpen
                onSave={(prompt) => update.mutate({ prompt })}
              />
              <NoRunsStrip ticket={t} authoring={authoring} onChange={invalidate} />
            </div>
          )}
        </section>

        {/* RIGHT — runs & evidence (xl only, and only once there are runs). */}
        {hasRuns && (
          <aside className="hidden border-l border-ink-700 px-4 py-4 xl:block xl:overflow-y-auto">
            {runsPanel}
          </aside>
        )}
      </div>
    </div>
  );
}

/** Slim zero-runs affordance: names the empty state in one line and offers the
 *  two start actions inline, instead of two empty run/evidence panels. */
function NoRunsStrip({
  ticket,
  authoring,
  onChange,
}: {
  ticket: TicketOut;
  authoring: boolean;
  onChange: () => void;
}) {
  const actions = useTicketActions();
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-card border border-dashed border-ink-700 bg-ink-900/40 px-4 py-3">
      <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full border border-ink-700 bg-ink-800 text-moon-600">
        <History size={13} />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-moon-100">No runs yet</p>
        <p className="text-xs text-moon-600">
          Start the first conversation — the transcript and evidence appear here.
        </p>
      </div>
      {authoring && (
        <div className="flex shrink-0 items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            leadingIcon={<Zap size={13} />}
            onClick={async () => {
              if (await actions.runNow(ticket)) onChange();
            }}
          >
            Run now
          </Button>
          {ticket.status === "draft" && (
            <Button
              size="sm"
              variant="ghost"
              onClick={async () => {
                if (await actions.transition(ticket.id, "queued")) onChange();
              }}
            >
              Queue
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

function RunsPanel({
  ticketId,
  runs,
  todos,
  subagents,
}: {
  ticketId: string;
  runs: RunOut[];
  todos: TodoItem[];
  subagents: SubagentSummary[];
}) {
  return (
    <div className="space-y-3">
      {/* Ticket-level progress from the latest run's transcript. */}
      <TasksPanel todos={todos} />
      <SubagentsPanel subagents={subagents} />
      <div>
        <h2 className="mb-2 font-display text-xs font-semibold uppercase tracking-wide text-moon-400">
          Runs {runs.length > 0 && <span className="text-moon-600">· {runs.length}</span>}
        </h2>
        <RunTimeline ticketId={ticketId} runs={runs} />
      </div>
    </div>
  );
}
