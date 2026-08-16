import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronRight, GitMerge } from "lucide-react";
import { qk } from "@/api";
import { useAcknowledge } from "@/api/ack";
import { ticketsApi } from "@/api/tickets";
import { useRuns, useRunDiffstat } from "@/api/runs";
import { useTicketExternalLinks } from "@/api/integrations";
import type { RunOut, TicketOut } from "@/api/types";
import { useKeybinds } from "@/lib/keymap";
import { useTicketActions } from "@/lib/ticketActions";
import { durationBetween, parseTs } from "@/lib/time";
import { formatUsd as fmtCost } from "@/lib/status";
import { toast } from "@/ui/Toast";
import { Kbd } from "@/ui/Kbd";
import { PriorityChip } from "@/components/PriorityChip";
import { UnackedDot } from "@/components/UnackedDot";
import { cn } from "@/lib/cn";
import { sortReview } from "./overview";

/**
 * NEEDS YOUR VERDICT — one row per review ticket with right-aligned run meta
 * (duration · cost · files · +/-), expanding in place to a per-file diffstat,
 * one-line run summary, retry note, and linked MR. Verbs: Approve (archive) and
 * Requeue (with an optional note that rides into the next run as context).
 *
 * Keyboard model (scoped to this section, disabled while typing in the note
 * field): j/k move the cursor, e expands, a approves, r requeues. The cursor
 * row scrolls smoothly into view as it moves. (docs/design/project-control-plane.md
 * §Overview ③.)
 */
export function VerdictSection({ tickets }: { tickets: TicketOut[] }) {
  const ordered = sortReview(tickets);
  const [cursor, setCursor] = useState(0);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);

  // Clamp the cursor when the list shrinks (a verdict action removes a row).
  // Every read uses safeCursor, so an out-of-range `cursor` is harmless until
  // the next j/k re-sets it — no sync effect needed.
  const safeCursor = ordered.length === 0 ? 0 : Math.min(cursor, ordered.length - 1);

  const scrollTo = (i: number) => {
    const el = rowRefs.current[i];
    el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  const at = ordered[safeCursor];

  useKeybinds(
    [
      {
        combo: "j",
        label: "Next verdict",
        group: "Overview",
        scope: "route",
        handler: () => {
          if (ordered.length === 0) return;
          const n = Math.min(safeCursor + 1, ordered.length - 1);
          setCursor(n);
          scrollTo(n);
        },
      },
      {
        combo: "k",
        label: "Previous verdict",
        group: "Overview",
        scope: "route",
        handler: () => {
          if (ordered.length === 0) return;
          const n = Math.max(safeCursor - 1, 0);
          setCursor(n);
          scrollTo(n);
        },
      },
      {
        combo: "e",
        label: "Expand verdict",
        group: "Overview",
        scope: "route",
        handler: (e) => {
          if (e.repeat || !at) return;
          setExpanded((prev) => {
            const next = new Set(prev);
            if (next.has(at.id)) next.delete(at.id);
            else next.add(at.id);
            return next;
          });
        },
      },
      {
        combo: "a",
        label: "Approve verdict",
        group: "Overview",
        scope: "route",
        handler: (e) => {
          // Suppress key auto-repeat so holding 'a' fires one archive, not a
          // burst of them. Fire via a data-attr hook on the cursor row so the
          // row owns its action state (loading/toast) and the keyboard path is
          // identical to the button path.
          if (e.repeat) return;
          rowRefs.current[safeCursor]
            ?.querySelector<HTMLButtonElement>("[data-verb='approve']")
            ?.click();
        },
      },
      {
        combo: "r",
        label: "Requeue verdict",
        group: "Overview",
        scope: "route",
        handler: (e) => {
          if (e.repeat) return;
          rowRefs.current[safeCursor]
            ?.querySelector<HTMLButtonElement>("[data-verb='requeue']")
            ?.click();
        },
      },
    ],
    ordered.length > 0,
  );

  if (ordered.length === 0) return null;

  return (
    <section id="ov-verdict" className="mb-7 scroll-mt-32">
      <SectionHead
        label="Needs your verdict"
        count={ordered.length}
        legend={
          <span className="hidden items-center gap-1 sm:inline-flex">
            <Kbd>j</Kbd>/<Kbd>k</Kbd> move · <Kbd>e</Kbd> expand · <Kbd>a</Kbd> approve ·{" "}
            <Kbd>r</Kbd> requeue
          </span>
        }
      />
      <div>
        {ordered.map((t, i) => (
          <div key={t.id} ref={(el) => { rowRefs.current[i] = el; }} className="border-b border-ink-900 last:border-b-0">
            <VerdictRow
              ticket={t}
              expanded={expanded.has(t.id)}
              onToggleExpand={() =>
                setExpanded((prev) => {
                  const next = new Set(prev);
                  if (next.has(t.id)) next.delete(t.id);
                  else next.add(t.id);
                  return next;
                })
              }
              isCursor={i === safeCursor}
            />
          </div>
        ))}
      </div>
    </section>
  );
}

function VerdictRow({
  ticket,
  expanded,
  onToggleExpand,
  isCursor,
}: {
  ticket: TicketOut;
  expanded: boolean;
  onToggleExpand: () => void;
  isCursor: boolean;
}) {
  const runsQ = useRuns(ticket.id);
  const runs = runsQ.data ?? [];
  const latest = latestRun(runs);
  const diffstatQ = useRunDiffstat(latest?.id);
  const actions = useTicketActions();
  const acknowledge = useAcknowledge();
  const qc = useQueryClient();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const diffstat = diffstatQ.data;
  const unacked = !ticket.acknowledged_at;

  function ack() {
    acknowledge.mutate(ticket.id, {
      onError: (err) => toast.error("Ack failed", { error: err }),
    });
  }

  async function requeueWithNote() {
    if (busy) return;
    setBusy(true);
    try {
      const trimmed = note.trim();
      if (trimmed) {
        await ticketsApi.setNextRunContext(ticket.id, { body: trimmed });
      }
      await ticketsApi.requeue(ticket.id);
      qc.invalidateQueries({ queryKey: qk.tickets.all });
      qc.invalidateQueries({ queryKey: ["inbox"] });
      toast.success("Ticket requeued", { description: trimmed ? "Note added to next run" : undefined });
    } catch (err) {
      toast.error("Requeue failed", { error: err });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className={cn(
        expanded ? "bg-warn/[0.04]" : "hover:bg-ink-900/50",
        isCursor && !expanded && "bg-ink-900/60",
      )}
    >
      {/* Collapsed head (also the top of the expanded block). */}
      <div
        className={cn(
          "flex items-center gap-2.5 px-2.5",
          expanded ? "pt-2.5" : "py-2",
        )}
      >
        <button
          onClick={onToggleExpand}
          className="flex shrink-0 items-center text-moon-600 hover:text-moon-300"
          aria-label={expanded ? "Collapse" : "Expand evidence"}
        >
          <ChevronRight
            size={14}
            className={cn("transition-transform", expanded && "rotate-90")}
          />
        </button>
        <PriorityChip value={ticket.priority} hideNone />
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-moon-100">
          {ticket.title}
        </span>
        {unacked && <UnackedDot className="mr-0.5" />}
        <span className="flex shrink-0 items-center gap-2.5 font-mono text-[11px] text-moon-600">
          {latest ? (
            <>
              <span>run {durationBetween(latest.started_at, latest.finished_at) || "—"}</span>
              <span>{fmtCost(latest.cost_usd)}</span>
              {diffstat && diffstat.total_files > 0 && (
                <span>
                  {diffstat.total_files}f{" "}
                  <span className="text-success">+{diffstat.total_added}</span>
                  <span className="text-failed"> −{diffstat.total_deleted}</span>
                </span>
              )}
            </>
          ) : runsQ.isLoading ? (
            <span>…</span>
          ) : (
            <span>no run</span>
          )}
        </span>
        {!expanded && (
          <span className="flex shrink-0 gap-1.5">
            {unacked && (
              <VerbButton tone="ghost" data-verb="ack" onClick={ack}>
                Ack
              </VerbButton>
            )}
            <VerbButton tone="sage" data-verb="approve" onClick={() => actions.archive(ticket)}>
              Approve
            </VerbButton>
            <VerbButton tone="ghost" data-verb="requeue" onClick={requeueWithNote} disabled={busy}>
              Requeue
            </VerbButton>
          </span>
        )}
      </div>

      {expanded && (
        <div className="px-2.5 pb-2.5 pt-1">
          <EvidenceBlock ticketId={ticket.id} latest={latest} runs={runs} />
          <div className="mt-2 flex items-center gap-1.5">
            {unacked && (
              <VerbButton tone="ghost" data-verb="ack" onClick={ack}>
                Ack
              </VerbButton>
            )}
            <VerbButton tone="sage" data-verb="approve" onClick={() => actions.archive(ticket)}>
              Approve
            </VerbButton>
            <VerbButton tone="ghost" data-verb="requeue" onClick={requeueWithNote} disabled={busy}>
              Requeue
            </VerbButton>
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  requeueWithNote();
                }
              }}
              placeholder="Add a note for the next run (optional)…"
              className="min-w-0 flex-1 rounded-control border border-ink-700 bg-ink-950 px-2.5 py-1.5 font-sans text-[12px] text-moon-100 placeholder:text-moon-600 focus:border-lamp/50 focus:outline-none"
            />
          </div>
        </div>
      )}
    </div>
  );
}

function EvidenceBlock({
  ticketId,
  latest,
  runs,
}: {
  ticketId: string;
  latest: RunOut | undefined;
  runs: RunOut[];
}) {
  // Fetched only when expanded (this block only renders then) — avoids fanning
  // out an external-links read for every collapsed review row on mount.
  const linksQ = useTicketExternalLinks(ticketId);
  const diffstatQ = useRunDiffstat(latest?.id);
  const diffstat = diffstatQ.data;
  const total = runs.length;
  const failed = runs.filter((r) => r.exit_status === "failed").length;

  const summary = latest
    ? latest.error_summary
      ? latest.error_summary
      : latest.exit_status === "failed"
        ? latest.failure_kind || "Run failed"
        : "Ran successfully"
    : "No run recorded";
  const mrLinks = (linksQ.data ?? []).filter((l) => l.kind === "merge_request");

  return (
    <div className="rounded-control border border-ink-800 bg-ink-950/70 p-2.5">
      <div className="flex flex-col gap-1.5 text-[11.5px]">
        <DetailRow label="Run summary">
          <span className="text-moon-300">{summary}</span>
        </DetailRow>
        <DetailRow label="Attempts">
          <span className="font-mono text-moon-600">
            {failed > 0 ? `${failed} failed · ${total} total` : `${total} run${total === 1 ? "" : "s"}`}
          </span>
        </DetailRow>
        {mrLinks.length > 0 && (
          <DetailRow label="Linked MR">
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-moon-600">
              {mrLinks.map((m) => (
                <span key={m.external_iid} className="inline-flex items-center gap-1">
                  <GitMerge size={11} className="text-review" />!{m.external_iid}
                  {m.state ? ` · ${m.state}` : ""}
                </span>
              ))}
            </span>
          </DetailRow>
        )}
      </div>

      {/* Per-file diff stat (the payload the /diffstat endpoint exists to feed). */}
      <div className="mt-2.5 border-t border-ink-800 pt-2">
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-moon-600">
          {diffstat ? `${diffstat.total_files} file${diffstat.total_files === 1 ? "" : "s"}` : "Files"}
        </div>
        {diffstatQ.isLoading ? (
          <div className="text-[11px] text-moon-600">Loading diff…</div>
        ) : diffstat && diffstat.files.length > 0 ? (
          <ul className="max-h-44 space-y-0.5 overflow-y-auto pr-1">
            {diffstat.files.map((f) => (
              <li key={f.path} className="flex items-center gap-2 font-mono text-[11px]">
                <span className="min-w-0 flex-1 truncate text-moon-400">{f.path}</span>
                {f.binary ? (
                  <span className="text-moon-600">binary</span>
                ) : (
                  <span className="shrink-0 tabular-nums">
                    <span className="text-success">+{f.additions}</span>{" "}
                    <span className="text-failed">−{f.deletions}</span>
                  </span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-[11px] text-moon-600">No file changes.</div>
        )}
      </div>
    </div>
  );
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-moon-600">{label}</span>
      <span className="min-w-0 text-right">{children}</span>
    </div>
  );
}

function latestRun(runs: RunOut[]): RunOut | undefined {
  if (runs.length === 0) return undefined;
  return [...runs].sort((a, b) => (parseTs(b.started_at) ?? 0) - (parseTs(a.started_at) ?? 0))[0];
}

// --- shared bits (also used by sibling sections via re-export) -----------------

export function SectionHead({
  label,
  count,
  legend,
}: {
  label: string;
  count: number;
  legend?: React.ReactNode;
}) {
  return (
    <div className="mb-1.5 flex items-baseline gap-2 border-b border-ink-800 pb-1.5">
      <span className="font-display text-[11.5px] font-bold uppercase tracking-[0.06em] text-moon-400">
        {label}
      </span>
      <span className="text-[11px] text-moon-600">({count})</span>
      {legend && <span className="ml-auto text-[10.5px] text-moon-600">{legend}</span>}
    </div>
  );
}

export function VerbButton({
  tone,
  children,
  ...rest
}: {
  tone: "sage" | "ghost" | "warn";
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...rest}
      className={cn(
        "rounded-control border px-2.5 py-1 text-[11px] font-medium transition-colors",
        tone === "sage" &&
          "border-success/40 bg-success/15 text-lamp-soft hover:bg-success/25",
        tone === "ghost" && "border-transparent text-moon-400 hover:bg-ink-800 hover:text-moon-100",
        tone === "warn" && "border-warn/50 bg-warn/15 text-warn hover:bg-warn/25",
        rest.disabled && "cursor-not-allowed opacity-50",
      )}
    >
      {children}
    </button>
  );
}
