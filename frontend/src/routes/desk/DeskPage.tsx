import { forwardRef, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import {
  Archive,
  ArrowRight,
  CheckCircle2,
  Inbox as InboxIcon,
  Moon,
  Plus,
  RotateCcw,
  Sparkles,
  Sun,
  Zap,
} from "lucide-react";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { EmptyState } from "@/ui/EmptyState";
import { Kbd } from "@/ui/Kbd";
import { StatusPill } from "@/ui/StatusPill";
import { PriorityChip } from "@/components/PriorityChip";
import { ProjectTag } from "@/components/ProjectDot";
import { RunningCard } from "./RunningCard";
import { useTickets } from "@/api/tickets";
import { useRuns } from "@/api/runs";
import { useInbox } from "@/api/inbox";
import { useAckDigest, useBulkAck } from "@/api/ack";
import type { AckDigestGroup } from "@/api/types";
import { useProjectMap } from "@/api/projects";
import type { ProjectOut, RunOut, TicketOut } from "@/api/types";
import { useTicketActions } from "@/lib/ticketActions";
import { useKeybinds } from "@/lib/keymap";
import { getLastVisit, setLastVisit } from "@/lib/lastVisit";
import { parseTs, relativeTime, durationBetween } from "@/lib/time";
import { formatUsd, runStatusKind } from "@/lib/status";
import { humanizeRunError } from "@/lib/runError";
import { Tooltip } from "@/ui/Tooltip";
import { ticketHref } from "@/lib/routes";
import { cn } from "@/lib/cn";
import { openComposer } from "@/components/composerBus";

const POLL = 3000;

function DeskBand({
  icon,
  title,
  accent,
  count,
  children,
}: {
  icon: ReactNode;
  title: string;
  accent?: boolean;
  count?: number;
  children: ReactNode;
}) {
  return (
    <section className="mb-7">
      <div className="mb-3 flex items-center gap-2">
        <span className={cn("text-moon-400", accent && "dawn-text")}>{icon}</span>
        <h2 className="font-display text-xs font-semibold uppercase tracking-wide text-moon-400">
          {title}
        </h2>
        {count != null && count > 0 && (
          <span className="rounded-full bg-ink-800 px-1.5 py-0.5 font-mono text-[10px] text-moon-400">
            {count}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

/** Slim one-line empty affordance for the Desk bands. Collapses a would-be
 *  ~350px empty card to ~48px so the whole Desk story stays above the fold when
 *  nothing is running or waiting. */
function DeskEmptyStrip({
  icon,
  text,
  action,
}: {
  icon?: ReactNode;
  text: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-2.5 rounded-card border border-dashed border-ink-700 bg-ink-900/40 px-3.5 py-3 text-sm text-moon-400">
      {icon && <span className="shrink-0 text-moon-600">{icon}</span>}
      <span className="min-w-0 flex-1 truncate">{text}</span>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/** Latest run per ticket id, from a newest-first runs list. */
function latestRunMap(runs: RunOut[]): Map<string, RunOut> {
  const m = new Map<string, RunOut>();
  for (const r of runs) if (!m.has(r.ticket_id)) m.set(r.ticket_id, r);
  return m;
}

interface NeedsItem {
  ticket: TicketOut;
  reason: "failed" | "review";
  run?: RunOut;
}

export function DeskPage() {
  const navigate = useNavigate();
  const actions = useTicketActions();
  const projects = useProjectMap();

  const review = useTickets({ status: "review" }, { refetchInterval: POLL });
  const running = useTickets({ status: "running" }, { refetchInterval: POLL });
  const runs = useRuns(undefined, { refetchInterval: POLL });
  const inbox = useInbox(null, { refetchInterval: POLL });

  // Capture the visit watermark once at mount; refresh it on unmount so the
  // "while you were away" feed reflects the gap since the previous session.
  const [visitAt] = useState(() => getLastVisit());
  useEffect(() => () => setLastVisit(), []);

  const runsList = runs.data ?? [];
  const latest = useMemo(() => latestRunMap(runsList), [runsList]);

  // NEEDS YOU: failed latest-runs first, then review-state tickets.
  const needs = useMemo<NeedsItem[]>(() => {
    const reviewTickets = review.data ?? [];
    const runningIds = new Set((running.data ?? []).map((t) => t.id));
    const items: NeedsItem[] = [];
    const seen = new Set<string>();

    // Failed runs whose ticket isn't currently re-running.
    for (const r of runsList) {
      if (!r.finished_at) continue;
      if (r.exit_status === "success") continue;
      if (runningIds.has(r.ticket_id)) continue;
      if (seen.has(r.ticket_id)) continue;
      const t = reviewTickets.find((x) => x.id === r.ticket_id);
      if (!t) continue; // only surface if it's sitting in review awaiting you
      seen.add(r.ticket_id);
      items.push({ ticket: t, reason: "failed", run: r });
    }
    // Remaining review tickets (succeeded / no run yet).
    for (const t of reviewTickets) {
      if (seen.has(t.id)) continue;
      seen.add(t.id);
      items.push({ ticket: t, reason: "review", run: latest.get(t.id) });
    }
    return items;
  }, [review.data, running.data, runsList, latest]);

  // Keyboard cursor over the Needs-You rows.
  const [cursor, setCursor] = useState(0);
  useEffect(() => {
    if (cursor >= needs.length) setCursor(Math.max(0, needs.length - 1));
  }, [needs.length, cursor]);

  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);
  useEffect(() => {
    rowRefs.current[cursor]?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const focused = needs[cursor];
  const openFocused = () =>
    focused && navigate({ to: "/tickets/$id", params: { id: focused.ticket.id } });
  useKeybinds([
    { combo: "j", label: "Cursor down", group: "Desk", handler: () => setCursor((c) => Math.min(needs.length - 1, c + 1)) },
    { combo: "k", label: "Cursor up", group: "Desk", handler: () => setCursor((c) => Math.max(0, c - 1)) },
    { combo: "Enter", label: "Open ticket", group: "Desk", handler: openFocused },
    { combo: "o", label: "Open ticket", group: "Desk", handler: openFocused },
    { combo: "r", label: "Requeue", group: "Desk", handler: () => focused && actions.requeue(focused.ticket) },
    { combo: "a", label: "Archive", group: "Desk", handler: () => focused && actions.archive(focused.ticket) },
  ]);

  const inboxCount = inbox.data?.length ?? 0;

  // WHILE YOU WERE AWAY: deltas since the visit watermark.
  const away = useMemo(() => {
    if (visitAt == null) return null;
    const finished = runsList.filter((r) => {
      const ts = parseTs(r.finished_at);
      return ts != null && ts > visitAt;
    });
    const enteredReview = (review.data ?? []).filter((t) => {
      const ts = parseTs(t.updated_at);
      return ts != null && ts > visitAt;
    });
    const newInbox = (inbox.data ?? []).filter((it) => {
      const ts = parseTs(it.ticket.created_at);
      return ts != null && ts > visitAt;
    });
    return { finished, enteredReview, newInbox };
  }, [visitAt, runsList, review.data, inbox.data]);

  return (
    <Page
      title="Desk"
      subtitle="What needs you, what's running, and what changed while you were away."
      width="xwide"
      actions={
        <Button variant="primary" leadingIcon={<Plus size={15} />} onClick={() => openComposer()}>
          New ticket
        </Button>
      }
    >
      <DeskBand icon={<Sun size={15} />} title="Needs you" count={needs.length}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Link
            to="/inbox"
            className="inline-flex items-center gap-2 rounded-control border border-ink-700 bg-ink-900 px-3 py-1.5 text-sm text-moon-100 hover:bg-ink-800"
          >
            <InboxIcon size={14} className="text-moon-400" />
            Inbox
            <span className="rounded-full bg-ink-800 px-1.5 py-0.5 font-mono text-[10px] text-moon-400">
              {inboxCount}
            </span>
          </Link>
          {needs.length > 0 && (
            <span className="hidden items-center gap-1.5 text-xs text-moon-600 md:flex">
              <Kbd>j</Kbd>
              <Kbd>k</Kbd> move
              <Kbd>o</Kbd> open
              <Kbd>r</Kbd> requeue
              <Kbd>a</Kbd> archive
            </span>
          )}
        </div>

        {needs.length === 0 ? (
          <EmptyState
            icon={<Sparkles size={18} />}
            title="Nothing waiting on you"
            description="Review-state tickets and failed runs land here with one-key actions."
            action={
              inboxCount > 0 ? (
                <Button asChild variant="ghost">
                  <Link to="/inbox">Triage {inboxCount} inbox items</Link>
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className="space-y-1.5">
            {needs.map((item, i) => (
              <NeedsRow
                key={item.ticket.id}
                ref={(el) => (rowRefs.current[i] = el)}
                item={item}
                project={item.ticket.project_id ? projects.get(item.ticket.project_id) : undefined}
                focused={i === cursor}
                onFocus={() => setCursor(i)}
                onOpen={() => navigate({ to: "/tickets/$id", params: { id: item.ticket.id } })}
                onRequeue={() => actions.requeue(item.ticket)}
                onArchive={() => actions.archive(item.ticket)}
              />
            ))}
          </div>
        )}
      </DeskBand>

      <ToAcknowledgeBand projects={projects} />

      <DeskBand icon={<Zap size={15} />} title="Running now" accent count={running.data?.length}>
        {running.data && running.data.length > 0 ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {running.data.map((t) => (
              <RunningCard
                key={t.id}
                ticket={t}
                run={latest.get(t.id)}
                project={t.project_id ? projects.get(t.project_id) : undefined}
              />
            ))}
          </div>
        ) : (
          <DeskEmptyStrip
            icon={<Zap size={14} />}
            text="No live runs — active runs surface here with elapsed time, ticking cost, model, and the latest transcript line."
          />
        )}
      </DeskBand>

      <DeskBand icon={<Moon size={15} />} title="While you were away">
        <AwayFeed away={away} projects={projects} />
      </DeskBand>
    </Page>
  );
}

// --- Needs You row -------------------------------------------------------------

const NeedsRow = forwardRef<
  HTMLDivElement,
  {
    item: NeedsItem;
    project?: ProjectOut;
    focused: boolean;
    onFocus: () => void;
    onOpen: () => void;
    onRequeue: () => void;
    onArchive: () => void;
  }
>(function NeedsRow({ item, project, focused, onFocus, onOpen, onRequeue, onArchive }, ref) {
  const { ticket, reason, run } = item;
  return (
    <div
      ref={ref}
      onMouseEnter={onFocus}
      className={cn(
        // Phone: pill+title stack on top, then the meta line, then actions.
        // md+: the original single row. md:contents on the pill+title wrapper
        // hoists them into the row so the layout is unchanged there.
        "group flex flex-col gap-2 rounded-control border px-3 py-2.5 shadow-[var(--shadow-raised)] transition-colors md:flex-row md:items-center md:gap-3",
        focused
          ? "border-lamp/40 bg-ink-800"
          : reason === "failed"
            ? "wash-failed border-failed/30 bg-ink-900"
            : "border-ink-700 bg-ink-900 hover:bg-ink-800",
      )}
    >
      <div className="flex min-w-0 items-start gap-2.5 md:contents md:items-center">
        {reason === "failed" ? (
          <StatusPill status="failed" label="Failed" />
        ) : (
          <StatusPill status="review" />
        )}
        <a
          href={ticketHref(ticket.id)}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey) return;
            e.preventDefault();
            onOpen();
          }}
          className="min-w-0 flex-1 rounded-[4px] text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
        >
          <span className="block text-sm font-medium text-moon-100 group-hover:text-lamp line-clamp-2 md:truncate">
            {ticket.description?.trim() || ticket.title}
          </span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-moon-600 md:flex-nowrap">
            <ProjectTag project={project} showNone />
            {run?.cost_usd != null && (
              <span className="font-mono tabular-nums">{formatUsd(run.cost_usd)}</span>
            )}
            {run?.finished_at && <span>{relativeTime(run.finished_at)}</span>}
            {reason === "failed" && run?.error_summary && (
              <Tooltip content={run.error_summary} mono>
                <span className="min-w-0 truncate text-failed">
                  {humanizeRunError(run.error_summary)}
                </span>
              </Tooltip>
            )}
          </span>
        </a>
      </div>
      <div className="flex shrink-0 items-center justify-end gap-1 opacity-80 group-hover:opacity-100">
        <PriorityChip value={ticket.priority} hideNone />
        {/* Open is redundant on a phone (tapping the title opens); requeue and
            archive collapse to icon-only below sm so the row never overflows. */}
        <Button size="sm" variant="ghost" onClick={onOpen} className="hidden sm:inline-flex">
          Open
        </Button>
        <Button
          size="sm"
          variant="subtle"
          leadingIcon={<RotateCcw size={13} />}
          onClick={onRequeue}
          aria-label="Requeue"
        >
          <span className="hidden sm:inline">Requeue</span>
        </Button>
        <Button
          size="sm"
          variant="ghost"
          leadingIcon={<Archive size={13} />}
          onClick={onArchive}
          aria-label="Archive"
        >
          <span className="hidden sm:inline">Archive</span>
        </Button>
      </div>
    </div>
  );
});

// --- To acknowledge ------------------------------------------------------------

/** Durable acknowledgement debt: agent-archived / agent-reviewed work the human
 *  never saw, grouped by project-day. Collapsed to group rows here; the full
 *  per-ticket digest with keyboard batch-ack lives at /acknowledge. Unlike
 *  "While you were away" (an ephemeral since-last-visit diff), this is
 *  server-backed and only clears when explicitly acknowledged. */
function ackDayLabel(day: string): string {
  const d = new Date(`${day}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((today.getTime() - d.getTime()) / 86_400_000);
  if (diff === 0) return "today";
  if (diff === 1) return "yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function ToAcknowledgeBand({ projects }: { projects: Map<string, ProjectOut> }) {
  const digest = useAckDigest(null, { refetchInterval: POLL });
  const bulkAck = useBulkAck();
  const total = digest.data?.total ?? 0;
  const groups = digest.data?.groups ?? [];
  const before = digest.data?.generated_at;

  if (total === 0) return null;

  const projName = (id: string | null) => (id ? projects.get(id)?.name ?? "No project" : "No project");
  const ackGroup = (group: AckDigestGroup) => {
    if (group.project_id != null) {
      bulkAck.mutate({ project_scope: true, project_id: group.project_id, before });
    } else {
      bulkAck.mutate({ ticket_ids: group.tickets.map((t) => t.ticket_id) });
    }
  };
  const ackAll = () => {
    for (const group of groups) ackGroup(group);
  };

  return (
    <DeskBand icon={<CheckCircle2 size={15} />} title="To acknowledge" count={total}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="subtle" onClick={ackAll}>
          Ack all ({total})
        </Button>
        <Button asChild size="sm" variant="ghost">
          <Link to="/acknowledge">Open digest</Link>
        </Button>
      </div>
      <div className="space-y-1.5">
        {groups.slice(0, 8).map((group) => (
          <div
            key={`${group.project_id ?? "none"}-${group.day}`}
            className="flex items-center gap-3 rounded-control border border-ink-700 bg-ink-900 px-3 py-2.5"
          >
            <span className="min-w-0 flex-1 truncate text-sm text-moon-100">
              {projName(group.project_id)}, {ackDayLabel(group.day)}
            </span>
            <span className="shrink-0 text-xs text-moon-600">
              {group.count} ticket{group.count === 1 ? "" : "s"} · {group.succeeded} ok
              {group.failed > 0 && <span className="text-failed"> · {group.failed} failed</span>}
              {group.cost_usd > 0 && <span> · {formatUsd(group.cost_usd)}</span>}
            </span>
            <Button size="sm" variant="ghost" onClick={() => ackGroup(group)} className="shrink-0">
              Ack
            </Button>
          </div>
        ))}
      </div>
    </DeskBand>
  );
}

// --- While you were away -------------------------------------------------------

function AwayFeed({
  away,
  projects,
}: {
  away: {
    finished: RunOut[];
    enteredReview: TicketOut[];
    newInbox: { ticket: TicketOut }[];
  } | null;
  projects: Map<string, ProjectOut>;
}) {
  if (!away) {
    return (
      <DeskEmptyStrip
        icon={<Moon size={14} />}
        text="First visit — after some runs finish, this band shows everything that changed since you were last here."
      />
    );
  }
  const total = away.finished.length + away.enteredReview.length + away.newInbox.length;
  if (total === 0) {
    return (
      <DeskEmptyStrip
        icon={<CheckCircle2 size={14} />}
        text="You're all caught up — nothing finished, entered review, or landed in the inbox since your last visit."
      />
    );
  }

  const projName = (id: string | null) => (id ? projects.get(id)?.name ?? "No project" : "No project");

  return (
    <div className="grid grid-cols-1 items-start gap-3 xl:grid-cols-2 2xl:grid-cols-3">
      {away.finished.length > 0 && (
        <div className="rounded-card border border-ink-700 bg-ink-900 p-3 shadow-[var(--shadow-raised)]">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-moon-600">
            {away.finished.length} run{away.finished.length === 1 ? "" : "s"} finished
          </div>
          <div className="space-y-1">
            {away.finished.slice(0, 12).map((r) => (
              <Link
                key={r.id}
                to="/tickets/$id/runs/$rid"
                params={{ id: r.ticket_id, rid: r.id }}
                className="flex items-center gap-3 rounded-control px-2 py-1.5 text-sm hover:bg-ink-800"
              >
                <StatusPill status={runStatusKind(r.exit_status)} />
                <span className="flex-1 truncate font-mono text-xs text-moon-400">
                  {r.model_used ?? "run"} · {durationBetween(r.started_at, r.finished_at)}
                </span>
                {r.cost_usd != null && (
                  <span className="font-mono text-xs tabular-nums text-moon-400">
                    {formatUsd(r.cost_usd)}
                  </span>
                )}
                <span className="text-xs text-moon-600">{relativeTime(r.finished_at)}</span>
              </Link>
            ))}
          </div>
        </div>
      )}

      {away.enteredReview.length > 0 && (
        <div className="rounded-card border border-ink-700 bg-ink-900 p-3 shadow-[var(--shadow-raised)]">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-moon-600">
            {away.enteredReview.length} entered review
          </div>
          <div className="space-y-1">
            {away.enteredReview.slice(0, 8).map((t) => (
              <Link
                key={t.id}
                to="/tickets/$id"
                params={{ id: t.id }}
                className="flex items-center gap-2 rounded-control px-2 py-1.5 text-sm hover:bg-ink-800"
              >
                <ArrowRight size={13} className="text-review" />
                <span className="flex-1 truncate text-moon-100">{t.title}</span>
                <span className="text-xs text-moon-600">{projName(t.project_id)}</span>
              </Link>
            ))}
          </div>
        </div>
      )}

      {away.newInbox.length > 0 && (
        <div className="rounded-card border border-ink-700 bg-ink-900 p-3 shadow-[var(--shadow-raised)]">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-moon-600">
            {away.newInbox.length} new in inbox
          </div>
          <div className="space-y-1">
            {away.newInbox.slice(0, 8).map(({ ticket: t }) => (
              <Link
                key={t.id}
                to="/inbox"
                className="flex items-center gap-2 rounded-control px-2 py-1.5 text-sm hover:bg-ink-800"
              >
                <InboxIcon size={13} className="text-moon-400" />
                <span className="flex-1 truncate text-moon-100">{t.title}</span>
                <span className="text-xs text-moon-600">{relativeTime(t.created_at)}</span>
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
