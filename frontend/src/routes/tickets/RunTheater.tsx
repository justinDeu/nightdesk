import type { ReactNode } from "react";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Cpu, Download, MessageSquareWarning } from "lucide-react";
import { useRun, runsApi } from "@/api/runs";
import { useTicket } from "@/api/tickets";
import { useRunComments, diffCommentsApi } from "@/api/diffComments";
import { qk } from "@/api/keys";
import { Button } from "@/ui/Button";
import { Badge } from "@/ui/Badge";
import { ErrorState } from "@/ui/ErrorState";
import { StatusPill } from "@/ui/StatusPill";
import { toast } from "@/ui/Toast";
import { LiveTranscript } from "@/components/LiveTranscript";
import { DiffView } from "@/components/DiffView";
import { runStatusKind, formatUsd, formatTokens } from "@/lib/status";
import { durationBetween, relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";

type Tab = "transcript" | "diff" | "log";

export function RunTheater() {
  const { id, rid } = useParams({ from: "/app/tickets/$id/runs/$rid" });
  const navigate = useNavigate();
  const search = useSearch({ from: "/app/tickets/$id/runs/$rid" });
  const tab: Tab = search.tab ?? "transcript";
  const setTab = (next: Tab) =>
    navigate({
      to: "/tickets/$id/runs/$rid",
      params: { id, rid },
      search: { tab: next },
      replace: true,
    });

  const ticketQ = useTicket(id);
  const runQ = useRun(rid, { refetchInterval: (q) => (q.state.data?.finished_at ? false : 3000) });
  const run = runQ.data;
  const ticket = ticketQ.data;

  // Per-run transcript endpoint decides liveness: finished runs replay, live
  // runs tail. No ticket-status assumption.
  const running = !!run && !run.finished_at;

  const log = useQuery({
    queryKey: ["runs", "detail", rid, "log"],
    queryFn: () => runsApi.log(rid),
    enabled: tab === "log",
    retry: false,
  });
  const diff = useQuery({
    queryKey: ["runs", "detail", rid, "diff"],
    queryFn: () => runsApi.diff(rid),
    enabled: tab === "diff",
    retry: false,
  });
  const queryClient = useQueryClient();
  const commentsQ = useRunComments(rid, { enabled: tab === "diff" });
  const comments = commentsQ.data ?? [];
  const unresolvedCount = comments.filter((c) => c.parent_id === null && !c.resolved).length;
  const invalidateComments = () =>
    queryClient.invalidateQueries({ queryKey: qk.runs.comments(rid) });

  const requestChanges = async () => {
    try {
      const res = await diffCommentsApi.requestChanges(rid);
      toast.success(
        `${unresolvedCount} comment${unresolvedCount === 1 ? "" : "s"} sent to next run`,
      );
      invalidateComments();
      queryClient.invalidateQueries({ queryKey: qk.tickets.detail(res.ticket_id) });
    } catch (err) {
      toast.error("Request changes failed", { error: err });
    }
  };

  const tokens =
    (run?.input_tokens ?? 0) + (run?.output_tokens ?? 0) + (run?.cache_read_tokens ?? 0);

  if (runQ.isError || (!runQ.isLoading && !run)) {
    return (
      <div className="grid h-full place-items-center px-4">
        <ErrorState
          className="w-full max-w-md"
          title="Run not found"
          description="This run may have been pruned, or the link is wrong. Open the ticket to see its current runs."
          action={
            <Button asChild variant="primary">
              <Link to="/tickets/$id" params={{ id }}>
                Back to ticket
              </Link>
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Sticky header */}
      <header className="sticky top-0 z-10 border-b border-ink-700 bg-ink-950/80 px-4 py-3 backdrop-blur sm:px-6">
        <div className="flex items-center gap-3">
          <Link
            to="/tickets/$id"
            params={{ id }}
            className="inline-flex items-center gap-1 text-sm text-moon-400 hover:text-moon-100"
          >
            <ArrowLeft size={15} /> Back
          </Link>
          <h1 className="min-w-0 flex-1 truncate font-display text-base font-semibold text-moon-100">
            {ticket?.title ?? "Run"}
          </h1>
          {run && <StatusPill status={runStatusKind(run.exit_status)} />}
        </div>
        {run && (
          <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 font-mono text-[11px] text-moon-400">
            <span className="rounded-full bg-ink-800 px-1.5 py-0.5 text-moon-400">{run.intent}</span>
            {run.model_used && (
              <span className="inline-flex items-center gap-1">
                <Cpu size={11} /> {run.model_used}
              </span>
            )}
            <span>{durationBetween(run.started_at, run.finished_at)}</span>
            <span className="tabular-nums text-lamp">{formatUsd(run.cost_usd)}</span>
            <span>{formatTokens(tokens)} tok</span>
            {run.input_tokens != null && (
              <span className="text-moon-600">
                in {formatTokens(run.input_tokens)} · out {formatTokens(run.output_tokens)} · cache{" "}
                {formatTokens(run.cache_read_tokens)}
              </span>
            )}
            <span className="text-moon-600">{relativeTime(run.started_at)}</span>
            <a
              href={`/api/v1/runs/${rid}/log`}
              target="_blank"
              rel="noreferrer"
              className="ml-auto inline-flex items-center gap-1 text-moon-400 hover:text-moon-100"
            >
              <Download size={12} /> Download log
            </a>
          </div>
        )}
        <div className="mt-2 flex items-center gap-1">
          <TabButton active={tab === "transcript"} onClick={() => setTab("transcript")}>
            Transcript
          </TabButton>
          <TabButton active={tab === "diff"} onClick={() => setTab("diff")}>
            Diff
            {unresolvedCount > 0 && (
              <Badge tone="review" className="ml-1.5">
                {unresolvedCount}
              </Badge>
            )}
          </TabButton>
          <TabButton active={tab === "log"} onClick={() => setTab("log")}>
            Worker log
          </TabButton>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-hidden px-4 py-4 sm:px-6">
        {tab === "transcript" ? (
          <LiveTranscript
            runId={rid}
            enabled
            running={running}
            caption={run?.model_used ?? undefined}
            className="h-full"
          />
        ) : tab === "diff" ? (
          diff.isLoading ? (
            <p className="text-sm text-moon-600">Loading diff…</p>
          ) : (
            <div className="flex h-full flex-col">
              {unresolvedCount > 0 && (
                <div className="mb-3 flex items-center gap-2 rounded-card border border-review/25 bg-review/[0.06] px-3 py-2">
                  <span className="text-[12px] text-moon-100">
                    {unresolvedCount} unresolved review comment{unresolvedCount === 1 ? "" : "s"}
                  </span>
                  <Button
                    size="sm"
                    variant="primary"
                    className="ml-auto"
                    leadingIcon={<MessageSquareWarning size={13} />}
                    onClick={requestChanges}
                  >
                    Request changes
                  </Button>
                </div>
              )}
              <div className="min-h-0 flex-1 overflow-auto">
                <DiffView
                  diff={diff.data}
                  runId={rid}
                  headSha={diff.data?.head_sha}
                  comments={comments}
                  onCommentsChange={invalidateComments}
                />
              </div>
            </div>
          )
        ) : log.isLoading ? (
          <p className="text-sm text-moon-600">Loading log…</p>
        ) : log.isError ? (
          <p className="text-sm text-moon-600">No worker log for this run.</p>
        ) : (
          <div className="h-full overflow-auto rounded-card border border-ink-700 bg-ink-950/40 p-3">
            <pre className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-moon-100">
              {log.data}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "rounded-control px-3 py-1 text-sm font-medium transition-colors",
        active ? "bg-ink-800 text-moon-100" : "text-moon-400 hover:text-moon-100",
      )}
    >
      {children}
    </button>
  );
}
