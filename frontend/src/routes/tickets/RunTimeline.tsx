import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Cpu, Download, Maximize2 } from "lucide-react";
import type { RunOut } from "@/api/types";
import { runsApi } from "@/api/runs";
import { StatusPill } from "@/ui/StatusPill";
import { DiffView } from "@/components/DiffView";
import { LiveTranscript } from "@/components/LiveTranscript";
import { runStatusKind, formatUsd, formatTokens } from "@/lib/status";
import { durationBetween, relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";

export function RunTimeline({ ticketId, runs }: { ticketId: string; runs: RunOut[] }) {
  if (runs.length === 0) {
    return (
      <p className="rounded-card border border-dashed border-ink-700 bg-ink-900/40 px-3 py-8 text-center text-sm text-moon-600">
        No runs yet. Queue or run this ticket to start the first conversation.
      </p>
    );
  }
  return (
    <div className="space-y-1.5">
      {runs.map((r) => (
        <RunRow key={r.id} ticketId={ticketId} run={r} />
      ))}
    </div>
  );
}

function RunRow({ ticketId, run }: { ticketId: string; run: RunOut }) {
  const [open, setOpen] = useState(false);
  const [showTx, setShowTx] = useState(false);
  const diff = useQuery({
    queryKey: ["runs", "detail", run.id, "diff"],
    queryFn: () => runsApi.diff(run.id),
    enabled: open,
    retry: false,
  });

  const tokens =
    (run.input_tokens ?? 0) + (run.output_tokens ?? 0) + (run.cache_read_tokens ?? 0);

  return (
    <div className="overflow-hidden rounded-card border border-ink-700 bg-ink-900">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-ink-800"
      >
        <ChevronRight
          size={13}
          className={cn("shrink-0 text-moon-600 transition-transform", open && "rotate-90")}
        />
        <StatusPill status={runStatusKind(run.exit_status)} />
        <span className="shrink-0 rounded-full bg-ink-800 px-1.5 py-0.5 text-[10px] text-moon-400">
          {run.intent}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-moon-400">
          {run.model_used && (
            <span className="inline-flex items-center gap-1">
              <Cpu size={10} /> {run.model_used}
            </span>
          )}
        </span>
        <span className="shrink-0 font-mono text-[11px] text-moon-600">
          {durationBetween(run.started_at, run.finished_at)}
        </span>
        {run.cost_usd != null && (
          <span className="shrink-0 font-mono text-[11px] tabular-nums text-moon-400">
            {formatUsd(run.cost_usd)}
          </span>
        )}
        <span className="hidden shrink-0 font-mono text-[11px] text-moon-600 sm:inline">
          {formatTokens(tokens)} tok
        </span>
        <span className="hidden shrink-0 text-[11px] text-moon-600 md:inline">
          {relativeTime(run.started_at)}
        </span>
      </button>

      {open && (
        <div className="space-y-3 border-t border-ink-700/60 bg-ink-950/40 px-3 py-3">
          <div className="flex flex-wrap items-center gap-4 text-[11px] text-moon-600">
            {run.error_summary && <span className="text-failed">{run.error_summary}</span>}
            {run.failure_kind && <span>failure: {run.failure_kind}</span>}
            <span className="font-mono">{run.host}</span>
            {run.started_as_run_now && <span className="text-lamp">run-now</span>}
          </div>

          <div className="flex items-center gap-2">
            <Link
              to="/tickets/$id/runs/$rid"
              params={{ id: ticketId, rid: run.id }}
              className="inline-flex items-center gap-1.5 rounded-control border border-ink-700 bg-ink-800 px-2.5 py-1 text-xs text-moon-100 hover:bg-ink-700"
            >
              <Maximize2 size={12} /> Open run theater
            </Link>
            <a
              href={`/api/v1/runs/${run.id}/log`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 rounded-control border border-ink-700 px-2.5 py-1 text-xs text-moon-400 hover:bg-ink-800 hover:text-moon-100"
            >
              <Download size={12} /> Log
            </a>
          </div>

          <div>
            <button
              onClick={() => setShowTx((v) => !v)}
              className="mb-1.5 inline-flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-moon-600 hover:text-moon-400"
            >
              <ChevronRight size={11} className={cn("transition-transform", showTx && "rotate-90")} />
              Transcript
            </button>
            {showTx && (
              <LiveTranscript
                runId={run.id}
                enabled
                running={!run.finished_at}
                caption={run.model_used ?? undefined}
                className="h-[40vh]"
              />
            )}
          </div>

          <div>
            <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
              Changes
            </div>
            {diff.isLoading ? (
              <p className="text-xs text-moon-600">Loading diff…</p>
            ) : diff.isError ? (
              <p className="text-xs text-moon-600">No diff available for this run.</p>
            ) : (
              <DiffView diff={diff.data} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
