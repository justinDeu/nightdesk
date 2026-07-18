import { forwardRef, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import {
  Archive,
  ArrowRight,
  Bot,
  CheckCheck,
  CheckCircle2,
  ChevronRight,
  HelpCircle,
  Inbox as InboxIcon,
  Moon,
  Plus,
  RotateCcw,
  Sparkles,
  Square,
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
import { TicketPeek } from "@/routes/tickets/TicketPeek";
import { peekBackdropClasses } from "@/routes/tickets/peekLayout";
import { useTicket, useTickets } from "@/api/tickets";
import { useRuns } from "@/api/runs";
import { useAgentAttention } from "@/api/agents";
import { useInbox } from "@/api/inbox";
import { useLabels } from "@/api/labels";
import { useAckDigest, useAcknowledge, useBulkAck } from "@/api/ack";
import type { AckDigestGroup, AckDigestTicket } from "@/api/types";
import { useProjectMap, useProjects } from "@/api/projects";
import type { ProjectOut, RunOut, TicketOut } from "@/api/types";
import { useTicketActions } from "@/lib/ticketActions";
import { useKeybinds } from "@/lib/keymap";
import { getLastVisit, setLastVisit } from "@/lib/lastVisit";
import { parseTs, relativeTime, durationBetween } from "@/lib/time";
import { formatUsd, formatTokens, runStatusKind } from "@/lib/status";
import { humanizeRunError } from "@/lib/runError";
import { Tooltip } from "@/ui/Tooltip";
import { ticketHref } from "@/lib/routes";
import { useNow } from "@/lib/useNow";
import { toast } from "@/ui/Toast";
import { cn } from "@/lib/cn";
import { openComposer } from "@/components/composerBus";

const POLL = 3000;

function DeskBand({
  icon,
  title,
  accent,
  count,
  className,
  children,
}: {
  icon: ReactNode;
  title: string;
  accent?: boolean;
  count?: number;
  /** Extra classes for the <section>, merged after the default mb-4. */
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={cn("mb-4", className)}>
      <div className="mb-2 flex items-center gap-2">
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

/** One-line description of what a blocked agent is waiting for. */
function pendingLabel(kind: string, tool: string | null): string {
  if (kind === "plan_approval") return "Waiting for plan approval";
  if (kind === "ask_question") return "Asked you a question";
  return tool ? `Needs permission to use ${tool}` : "Needs your input";
}

/** Latest run per ticket id, from a newest-first runs list. */
function latestRunMap(runs: RunOut[]): Map<string, RunOut> {
  const m = new Map<string, RunOut>();
  for (const r of runs) if (!m.has(r.ticket_id)) m.set(r.ticket_id, r);
  return m;
}

interface NeedsItem {
  ticket: TicketOut;
  reason: "failed" | "canceled" | "review";
  run?: RunOut;
}

/** Shared classes for a pulse-strip stat chip — a flat, borderless inline
 *  link that sits in one compact row separated from its neighbors by the
 *  strip's divide-x hairlines. */
const chipCls =
  "flex h-full items-center gap-1.5 px-3 first:pl-0 text-moon-100 hover:bg-ink-800/60";

/** One stat chip's content: a big-ish mono number with a tiny uppercase
 *  label to its right. `accent` lights a non-zero count so the eye lands on
 *  what needs attention. */
function StatValue({
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
          "font-mono text-lg font-semibold leading-none tabular-nums",
          accent ? "text-lamp" : "text-moon-100",
        )}
      >
        {value}
      </span>
      <span className="text-[10px] font-medium uppercase tracking-wide text-moon-600">{label}</span>
    </>
  );
}

export function DeskPage() {
  const navigate = useNavigate();
  const actions = useTicketActions();
  const acknowledge = useAcknowledge();
  const projects = useProjectMap();
  const projectsQ = useProjects();
  const labelsQ = useLabels();

  // Side-peek for tickets opened from the action bands. Tickets never
  // navigate away from the Desk — inspection happens in the same peek panel
  // the Tickets board uses.
  const [peekId, setPeekId] = useState<string | null>(null);
  const peekQ = useTicket(peekId ?? undefined);
  const peekTicket = peekId ? peekQ.data : undefined;

  const review = useTickets({ status: "review" }, { refetchInterval: POLL });
  const running = useTickets({ status: "running" }, { refetchInterval: POLL });
  const runs = useRuns(undefined, { refetchInterval: POLL });
  const inbox = useInbox(null, { refetchInterval: POLL });
  // Structured needs-input + unread replies, one poll (queryKey shared with
  // the nav badge so the Desk doesn't double-fetch).
  const attention = useAgentAttention();
  const attentionPending = attention.data?.pending ?? [];
  const attentionUnread = attention.data?.unread ?? [];
  const attentionTotal = attention.data?.total ?? 0;

  // Capture the visit watermark once at mount; refresh it on unmount so the
  // "while you were away" feed reflects the gap since the previous session.
  const [visitAt] = useState(() => getLastVisit());
  useEffect(() => () => setLastVisit(), []);

  const runsList = runs.data ?? [];
  const latest = useMemo(() => latestRunMap(runsList), [runsList]);

  // NEEDS YOU: failed latest-runs first, then review-state tickets. Reason is
  // derived from each ticket's LATEST run only — an old failure buried behind
  // a newer success must not flag the ticket as failed.
  const needs = useMemo<NeedsItem[]>(() => {
    const reviewTickets = review.data ?? [];
    const runningIds = new Set((running.data ?? []).map((t) => t.id));
    const failedItems: NeedsItem[] = [];
    const restItems: NeedsItem[] = [];

    for (const t of reviewTickets) {
      const run = latest.get(t.id);
      if (run && !runningIds.has(t.id)) {
        const kind = runStatusKind(run.exit_status);
        if (kind === "failed" || kind === "canceled") {
          failedItems.push({ ticket: t, reason: kind, run });
          continue;
        }
      }
      restItems.push({ ticket: t, reason: "review", run });
    }
    return [...failedItems, ...restItems];
  }, [review.data, running.data, latest]);

  // Keyboard cursor over the Needs-You rows. Mouse hover never moves this —
  // it's a separate, purely-CSS affordance (see NeedsRow) — and the ring only
  // renders once the cursor has actually been driven by the keyboard, so a
  // stray mount at index 0 doesn't paint a ring nobody asked for.
  const [cursor, setCursor] = useState(0);
  const [cursorActive, setCursorActive] = useState(false);
  useEffect(() => {
    if (cursor >= needs.length) setCursor(Math.max(0, needs.length - 1));
  }, [needs.length, cursor]);

  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);
  useEffect(() => {
    if (cursorActive) rowRefs.current[cursor]?.scrollIntoView({ block: "nearest" });
  }, [cursor, cursorActive]);

  // Combined resolve: stamp the ack checkpoint first, then archive. Archive
  // only fires once the ack landed so a failure can't leave an archived-but-
  // silently-unacked ticket; each half keeps its own error toast (archive via
  // useTicketActions, ack surfaced here since useAcknowledge has none).
  const ackAndArchive = async (t: TicketOut) => {
    try {
      await acknowledge.mutateAsync(t.id);
    } catch (err) {
      toast.error("Ack failed", { error: err });
      return;
    }
    await actions.archive(t);
  };

  const focused = needs[cursor];
  const openFocused = () => focused && setPeekId(focused.ticket.id);
  useKeybinds([
    {
      combo: "j",
      label: "Cursor down",
      group: "Desk",
      handler: () => {
        setCursorActive(true);
        setCursor((c) => Math.min(needs.length - 1, c + 1));
      },
    },
    {
      combo: "k",
      label: "Cursor up",
      group: "Desk",
      handler: () => {
        setCursorActive(true);
        setCursor((c) => Math.max(0, c - 1));
      },
    },
    { combo: "Enter", label: "Open ticket", group: "Desk", handler: openFocused },
    { combo: "o", label: "Open ticket", group: "Desk", handler: openFocused },
    { combo: "r", label: "Requeue", group: "Desk", handler: () => focused && actions.requeue(focused.ticket) },
    { combo: "a", label: "Archive", group: "Desk", handler: () => focused && actions.archive(focused.ticket) },
    { combo: "Escape", label: "Close peek", group: "Desk", handler: () => setPeekId(null) },
  ]);

  const inboxCount = inbox.data?.length ?? 0;
  const runningCount = running.data?.length ?? 0;

  // Spend today: cost of runs that started today (the stat-tile summary).
  const spendToday = useMemo(() => {
    const now = new Date();
    const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    let sum = 0;
    for (const r of runsList) {
      const ts = parseTs(r.started_at);
      if (ts != null && ts >= startOfDay) sum += r.cost_usd ?? 0;
    }
    return sum;
  }, [runsList]);

  // WHILE YOU WERE AWAY: runs that finished since the visit watermark. Review
  //  and inbox deltas are surfaced by the Needs-you band and the Inbox chip,
  //  so the away feed is runs-only — one compact line each.
  const away = useMemo(() => {
    if (visitAt == null) return null;
    return runsList.filter((r) => {
      const ts = parseTs(r.finished_at);
      return ts != null && ts > visitAt;
    });
  }, [visitAt, runsList]);

  return (
    <Page
      title="Desk"
      subtitle="What needs you, what's running, and what changed while you were away."
      bleed
      actions={
        <Button variant="primary" leadingIcon={<Plus size={15} />} onClick={() => openComposer()}>
          New ticket
        </Button>
      }
    >
      {/* Pulse strip — one flat row of stat chips (≤44px), each a real link,
          separated by hairline dividers with a bottom hairline. The dashboard
          archetype's stat row, compacted. */}
      <div className="mb-4 flex h-11 items-stretch divide-x divide-ink-700/60 border-b border-ink-700/60">
        <Link to="/tickets" search={{ f: "status:running" }} className={chipCls}>
          <StatValue label="Running" value={runningCount} accent={runningCount > 0} />
        </Link>
        <Link to="/tickets" search={{ f: "status:review" }} className={chipCls}>
          <StatValue label="Needs you" value={needs.length} accent={needs.length > 0} />
        </Link>
        <Link to="/inbox" className={chipCls}>
          <StatValue label="Inbox" value={inboxCount} accent={inboxCount > 0} />
        </Link>
        <Link to="/agents" className={chipCls}>
          <StatValue label="Agents" value={attentionTotal} accent={attentionTotal > 0} />
        </Link>
        <Link to="/analytics" className={chipCls}>
          <StatValue label="Today" value={formatUsd(spendToday)} />
        </Link>
      </div>

      {/* Two-column dashboard. The two most important signals — "Running now"
          and "While you were away" — live in a persistent awareness rail. The
          rail is first in the DOM, but on mobile the main column carries
          order-first so Needs-you leads the phone; explicit grid placement
          parks the rail on the right at lg+. The rail NEVER scrolls itself:
          it is content-sized and sticky, and every list inside it is capped by
          count (Running ≤6, Away ≤8) with a "+N more" link — so both band
          headers and their items stay visible with zero nested scrollbars. */}
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(300px,340px)] lg:gap-x-6">
        <aside className="min-w-0 lg:sticky lg:top-4 lg:col-start-2 lg:row-start-1 lg:self-start lg:border-l lg:border-ink-700/60 lg:pl-6">
          <DeskBand icon={<Zap size={15} />} title="Running now" accent count={runningCount}>
            {runningCount > 0 ? (
              <div className="space-y-0.5">
                {(running.data ?? []).slice(0, 6).map((t) => (
                  <RunningRow key={t.id} ticket={t} run={latest.get(t.id)} />
                ))}
                {runningCount > 6 && (
                  <Link
                    to="/tickets"
                    search={{ f: "status:running" }}
                    className="flex h-7 items-center gap-1.5 rounded-control px-2 text-xs text-moon-500 hover:bg-ink-800 hover:text-moon-200"
                  >
                    +{runningCount - 6} more running
                    <ArrowRight size={12} />
                  </Link>
                )}
              </div>
            ) : (
              <DeskEmptyStrip
                icon={<Zap size={14} />}
                text="No live runs — start one and it surfaces here live."
                action={
                  <Button
                    size="sm"
                    variant="subtle"
                    leadingIcon={<Plus size={13} />}
                    onClick={() => openComposer()}
                  >
                    New ticket
                  </Button>
                }
              />
            )}
          </DeskBand>

          <DeskBand icon={<Moon size={15} />} title="While you were away">
            <AwayFeed away={away} />
          </DeskBand>
        </aside>

        <div className="order-first min-w-0 lg:order-none lg:col-start-1 lg:row-start-1">
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
                    cursored={cursorActive && i === cursor}
                    onOpen={() => setPeekId(item.ticket.id)}
                    onAck={() => acknowledge.mutate(item.ticket.id)}
                    onAckArchive={() => ackAndArchive(item.ticket)}
                    onRequeue={() => actions.requeue(item.ticket)}
                    onArchive={() => actions.archive(item.ticket)}
                  />
                ))}
              </div>
            )}
          </DeskBand>

          {/* Seam E order in the action center: blocking agents first, then
              acknowledgement debt. The live-runs band moved to the right
              rail above. */}
          {attentionTotal > 0 && (
            <DeskBand
              icon={<HelpCircle size={15} />}
              title="Agents waiting on you"
              accent
              count={attentionTotal}
            >
              <div className="space-y-1.5">
                {/* Structured asks first — a blocked agent outranks an unread reply. */}
                {attentionPending.map((p) => (
                  <Link
                    key={p.id}
                    to="/agents/$id"
                    params={{ id: p.session_id }}
                    className="group flex items-center gap-3 rounded-control border border-lamp/30 bg-lamp/[0.05] px-3 py-2.5 transition-colors hover:bg-lamp/[0.09]"
                  >
                    <Bot size={15} className="shrink-0 text-lamp" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-moon-100 group-hover:text-lamp">
                        {p.session_title}
                      </span>
                      <span className="text-xs text-moon-500">{pendingLabel(p.kind, p.tool)}</span>
                    </span>
                    <span className="rounded-full bg-lamp/15 px-2 py-0.5 text-[11px] font-medium text-lamp">
                      Answer
                    </span>
                  </Link>
                ))}
                {/* Unread replies: the agent finished and idled — dimmer accent
                    than a hard ask, click opens the conversation (which marks it
                    seen). */}
                {attentionUnread.map((u) => (
                  <Link
                    key={u.session_id}
                    to="/agents/$id"
                    params={{ id: u.session_id }}
                    className="group flex items-center gap-3 rounded-control border border-ink-600 bg-ink-900/60 px-3 py-2.5 transition-colors hover:bg-ink-800/70"
                  >
                    <Bot size={15} className="shrink-0 text-moon-400" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-moon-100 group-hover:text-lamp">
                        {u.session_title}
                      </span>
                      <span className="block truncate text-xs text-moon-500">
                        replied · waiting on you
                        {u.preview ? <span className="text-moon-600"> — {u.preview}</span> : null}
                      </span>
                    </span>
                    <span className="rounded-full bg-ink-800 px-2 py-0.5 text-[11px] font-medium text-moon-400">
                      Read
                    </span>
                  </Link>
                ))}
              </div>
            </DeskBand>
          )}

          <ToAcknowledgeBand projects={projects} onPeek={setPeekId} />
        </div>
      </div>

      {peekTicket && (
        <>
          <div aria-hidden className={peekBackdropClasses} onClick={() => setPeekId(null)} />
          <TicketPeek
            key={peekTicket.id}
            ticket={peekTicket}
            project={peekTicket.project_id ? projects.get(peekTicket.project_id) : undefined}
            latestRun={latest.get(peekTicket.id)}
            projects={projectsQ.data ?? []}
            labels={labelsQ.data ?? []}
            onClose={() => setPeekId(null)}
            onOpenFull={() => navigate({ to: "/tickets/$id", params: { id: peekTicket.id } })}
          />
        </>
      )}
    </Page>
  );
}

// --- Running row ---------------------------------------------------------------

/** One running ticket as a single 32px row: [lamp dot][title, truncate][elapsed,
 *  mono][cost, mono]. The whole row is a real anchor to the ticket — click
 *  opens the detail (where Watch/Cancel live), middle/cmd-click opens a new
 *  tab — exactly the NeedsRow pattern. No transcript box, no buttons at rest;
 *  hover reveals a single cancel icon-button. The rail never hosts a scroll,
 *  so the caller caps how many of these it renders. */
function RunningRow({ ticket, run }: { ticket: TicketOut; run: RunOut | undefined }) {
  const navigate = useNavigate();
  const actions = useTicketActions();
  const now = useNow(true);
  const elapsed = run ? durationBetween(run.started_at, run.finished_at, now) : "";
  const cost = run?.cost_usd ?? null;
  return (
    <div className="group flex h-8 items-center gap-2 rounded-control px-2 hover:bg-ink-800/70">
      <a
        href={ticketHref(ticket.id)}
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey) return;
          e.preventDefault();
          navigate({ to: "/tickets/$id", params: { id: ticket.id } });
        }}
        className="flex min-w-0 flex-1 items-center gap-2.5 rounded-[4px] text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
      >
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-lamp" aria-hidden />
        <span className="min-w-0 flex-1 truncate text-sm text-moon-100 group-hover:text-lamp">
          {ticket.title}
        </span>
        <span className="shrink-0 font-mono text-xs tabular-nums text-lamp">{elapsed}</span>
        {cost != null && (
          <span className="shrink-0 font-mono text-xs tabular-nums text-moon-400">{formatUsd(cost)}</span>
        )}
      </a>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          actions.cancel(ticket);
        }}
        aria-label="Cancel run"
        className="hidden shrink-0 items-center justify-center rounded-control p-1 text-moon-500 hover:bg-ink-700 hover:text-failed group-hover:flex focus-visible:flex"
      >
        <Square size={13} />
      </button>
    </div>
  );
}

// --- Needs You row -------------------------------------------------------------

const NeedsRow = forwardRef<
  HTMLDivElement,
  {
    item: NeedsItem;
    project?: ProjectOut;
    /** Keyboard (j/k) cursor is on this row. Independent of hover — see the
     *  className below for why the two must never share a signal. */
    cursored: boolean;
    onOpen: () => void;
    onAck: () => void;
    onAckArchive: () => void;
    onRequeue: () => void;
    onArchive: () => void;
  }
>(function NeedsRow({ item, project, cursored, onOpen, onAck, onAckArchive, onRequeue, onArchive }, ref) {
  const { ticket, reason, run } = item;
  return (
    <div
      ref={ref}
      className={cn(
        // Phone: pill+title stack on top, then the meta line, then actions.
        // md+: the original single row. md:contents on the pill+title wrapper
        // hoists them into the row so the layout is unchanged there.
        "group relative flex flex-col gap-2 rounded-control border px-3 py-2.5 shadow-[var(--shadow-raised)] transition-colors md:flex-row md:items-center md:gap-3 hover:bg-ink-800",
        // State tint always wins the border/background — hover only ever adds
        // a neutral bg (above), it never swaps the border for the accent
        // color, so a failed row stays visibly failed while hovered.
        reason === "failed" ? "wash-failed border-failed/30 bg-ink-900" : "border-ink-700 bg-ink-900",
        // Keyboard cursor is a distinct signal from hover: an inset ring that
        // layers on top of (rather than replaces) the state border, matching
        // the Tickets list's focusedId ring convention.
        cursored && "ring-1 ring-inset ring-lamp/40",
      )}
    >
      <a
        href={ticketHref(ticket.id)}
        aria-label={ticket.title}
        onClick={(e) => {
          if (e.button !== 0 || e.metaKey || e.ctrlKey) return;
          e.preventDefault();
          onOpen();
        }}
        className="absolute inset-0 z-0 rounded-control focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-lamp"
      />
      <div className="pointer-events-none relative z-[1] flex min-w-0 items-start gap-2.5 md:contents md:items-center">
        {reason === "failed" ? (
          <StatusPill status="failed" label="Failed" />
        ) : reason === "canceled" ? (
          <StatusPill status="canceled" />
        ) : (
          <StatusPill status="review" />
        )}
        <div className="min-w-0 flex-1 text-left">
          {/* Title + meta stack on narrow screens; on wide screens they share
              one row — title bounded, meta filling the dead middle a truncated
              title otherwise leaves next to the actions. Telemetry (duration,
              tokens) is xl-only since it only earns its place once inline. */}
          <div className="flex flex-col gap-0.5 xl:flex-row xl:items-center xl:gap-3">
            <span
              className={cn(
                "block min-w-0 text-sm font-medium text-moon-100 line-clamp-2 md:truncate xl:max-w-[40%]",
                // Accent title-hover is for review rows only. On a failed or
                // canceled row the red/amber wash must stay coherent, so hover
                // merely brightens the title instead of recoloring it green.
                reason === "review" ? "group-hover:text-lamp" : "group-hover:text-moon-50",
              )}
            >
              {ticket.description?.trim() || ticket.title}
            </span>
            {/* min-w-0 + overflow-hidden: from md up the meta is nowrap, so it
                must shrink and clip inside its box — without this its content
                overflows invisibly and the z-10 action cluster paints on top
                of the text. Variable-length pieces (project name, error
                summary) truncate; fixed telemetry chips are shrink-0. */}
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 overflow-hidden text-xs text-moon-600 md:flex-nowrap xl:flex-1">
              <ProjectTag project={project} showNone className="min-w-0" />
              {run && (
                <>
                  <span className="hidden shrink-0 font-mono text-[11px] tabular-nums xl:inline">
                    {durationBetween(run.started_at, run.finished_at)}
                  </span>
                  <span className="hidden shrink-0 font-mono text-[11px] tabular-nums xl:inline">
                    {formatTokens((run.input_tokens ?? 0) + (run.output_tokens ?? 0))} tok
                  </span>
                </>
              )}
              {run?.cost_usd != null && (
                <span className="shrink-0 font-mono tabular-nums">{formatUsd(run.cost_usd)}</span>
              )}
              {run?.finished_at && (
                <span className="shrink-0">{relativeTime(run.finished_at)}</span>
              )}
              {reason === "failed" && run?.error_summary && (
                <Tooltip content={run.error_summary} mono>
                  <span className="min-w-0 truncate text-failed">
                    {humanizeRunError(run.error_summary)}
                  </span>
                </Tooltip>
              )}
            </span>
          </div>
        </div>
      </div>
      <div className="relative z-10 flex shrink-0 items-center justify-end gap-1 opacity-80 group-hover:opacity-100">
        <PriorityChip value={ticket.priority} hideNone />
        {/* One variant family (ghost), ordered by escalating finality:
            inspect (Open), re-run (Requeue), then the resolution verbs
            grouped at the end with plain Archive last. Open is redundant on
            a phone (tapping the title opens); requeue and archive collapse
            to icon-only below sm so the row never overflows. */}
        <Button size="sm" variant="ghost" onClick={onOpen} className="hidden sm:inline-flex">
          Open
        </Button>
        <Button
          size="sm"
          variant="ghost"
          leadingIcon={<RotateCcw size={13} />}
          onClick={onRequeue}
          aria-label="Requeue"
        >
          <span className="hidden sm:inline">Requeue</span>
        </Button>
        {ticket.acknowledged_at == null && (
          <>
            <Button size="sm" variant="ghost" onClick={onAck}>
              Ack
            </Button>
            {/* One-shot resolve for verified work. Icon-only at every width —
                the full label starves the title even at xl — with the styled
                tooltip carrying the name. */}
            <Tooltip content="Ack & archive">
              <Button
                size="sm"
                variant="ghost"
                leadingIcon={<CheckCheck size={13} />}
                onClick={onAckArchive}
                aria-label="Ack & archive"
              />
            </Tooltip>
          </>
        )}
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

/** Durable acknowledgement debt: decided (archived) work the human
 *  never saw, grouped by project-day. Each group row expands in place to its
 *  tickets (the digest already carries them — no extra fetch): outcome, title,
 *  cost, when it landed, plus a per-ticket Ack. Clicking a ticket opens the
 *  side-peek; nothing here navigates away from the Desk. Unlike "While you
 *  were away" (an ephemeral since-last-visit diff), this is server-backed and
 *  only clears when explicitly acknowledged. */
function ackDayLabel(day: string): string {
  const d = new Date(`${day}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((today.getTime() - d.getTime()) / 86_400_000);
  if (diff === 0) return "today";
  if (diff === 1) return "yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function ToAcknowledgeBand({
  projects,
  onPeek,
}: {
  projects: Map<string, ProjectOut>;
  onPeek: (ticketId: string) => void;
}) {
  const digest = useAckDigest(null, { refetchInterval: POLL });
  const bulkAck = useBulkAck();
  const acknowledge = useAcknowledge();
  const total = digest.data?.total ?? 0;
  const groups = digest.data?.groups ?? [];

  // Expanded project-day groups, keyed like the row key. Survives digest
  // re-polls (the key is stable) and quietly drops with the group when a
  // group empties out.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  if (total === 0) return null;

  const projName = (id: string | null) => (id ? projects.get(id)?.name ?? "No project" : "No project");
  const ackGroup = (group: AckDigestGroup) => {
    // Each group row is a (project, day) pair, so ack exactly the tickets the
    // digest already carries for it — a project_scope ack would clear every
    // unacked ticket in the project across all days. Explicit ids are also
    // inherently race-safe (no generated_at watermark needed).
    bulkAck.mutate({ ticket_ids: group.tickets.map((t) => t.ticket_id) });
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
      </div>
      <div className="space-y-1.5">
        {groups.slice(0, 8).map((group) => {
          const key = `${group.project_id ?? "none"}-${group.day}`;
          const open = expanded.has(key);
          return (
            <div
              key={key}
              className="overflow-hidden rounded-control border border-ink-700 bg-ink-900"
            >
              <div
                role="button"
                tabIndex={0}
                aria-expanded={open}
                onClick={() => toggle(key)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggle(key);
                  }
                }}
                className="flex cursor-pointer items-center gap-2 px-3 py-2.5 transition-colors hover:bg-ink-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-lamp"
              >
                <ChevronRight
                  size={14}
                  className={cn(
                    "shrink-0 text-moon-500 transition-transform",
                    open && "rotate-90",
                  )}
                />
                <span className="min-w-0 flex-1 truncate text-sm text-moon-100">
                  {projName(group.project_id)}, {ackDayLabel(group.day)}
                </span>
                <span className="shrink-0 text-xs text-moon-600">
                  {group.count} ticket{group.count === 1 ? "" : "s"} · {group.succeeded} ok
                  {group.failed > 0 && <span className="text-failed"> · {group.failed} failed</span>}
                  {group.cost_usd > 0 && <span> · {formatUsd(group.cost_usd)}</span>}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={(e) => {
                    e.stopPropagation();
                    ackGroup(group);
                  }}
                  className="shrink-0"
                >
                  Ack
                </Button>
              </div>
              {open && (
                <div className="border-t border-ink-700/60 bg-ink-950/40">
                  {group.tickets.map((t) => (
                    <AckTicketRow
                      key={t.ticket_id}
                      ticket={t}
                      onPeek={() => onPeek(t.ticket_id)}
                      onAck={() => acknowledge.mutate(t.ticket_id)}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })}
        {groups.length > 8 && (
          <div className="px-3 py-2 text-xs text-moon-600">
            +{groups.length - 8} more groups — ack a group above to reveal
          </div>
        )}
      </div>
    </DeskBand>
  );
}

/** One digest ticket inside an expanded group: outcome, title, cost, when it
 *  landed. Clicking opens the side-peek (never navigates); Ack clears just
 *  this ticket. */
function AckTicketRow({
  ticket,
  onPeek,
  onAck,
}: {
  ticket: AckDigestTicket;
  onPeek: () => void;
  onAck: () => void;
}) {
  return (
    <div className="group flex items-center gap-3 border-t border-ink-700/40 px-3 py-2 first:border-t-0 transition-colors hover:bg-ink-800/60">
      {ticket.outcome === "failed" ? (
        <StatusPill status="failed" label="Failed" />
      ) : (
        <StatusPill status={ticket.status === "archived" ? "archived" : "review"} />
      )}
      <a
        href={ticketHref(ticket.ticket_id)}
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey) return;
          e.preventDefault();
          onPeek();
        }}
        className="min-w-0 flex-1 rounded-[4px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
      >
        <span className="block truncate text-sm text-moon-100 group-hover:text-lamp">
          {ticket.description?.trim() || ticket.title}
        </span>
      </a>
      <span className="flex shrink-0 items-center gap-2 text-xs text-moon-600">
        {ticket.cost_usd != null && (
          <span className="font-mono tabular-nums">{formatUsd(ticket.cost_usd)}</span>
        )}
        {ticket.entered_at && <span>{relativeTime(ticket.entered_at)}</span>}
      </span>
      <Button size="sm" variant="ghost" onClick={onAck} className="shrink-0">
        Ack
      </Button>
    </div>
  );
}

// --- While you were away -------------------------------------------------------

function AwayFeed({ away }: { away: RunOut[] | null }) {
  if (!away) {
    return (
      <DeskEmptyStrip
        icon={<Moon size={14} />}
        text="First visit — after some runs finish, this band shows what changed since you were last here."
        action={
          <Button asChild size="sm" variant="ghost">
            <Link to="/tickets">Go to Tickets</Link>
          </Button>
        }
      />
    );
  }
  if (away.length === 0) {
    return (
      <DeskEmptyStrip
        icon={<CheckCircle2 size={14} />}
        text="You're all caught up — nothing finished since your last visit."
        action={
          <Button asChild size="sm" variant="ghost">
            <Link to="/tickets">Go to Tickets</Link>
          </Button>
        }
      />
    );
  }

  // Flat, one-line, capped by count — the rail never scrolls internally.
  const CAP = 8;
  const shown = away.slice(0, CAP);
  const more = away.length - shown.length;
  return (
    <div className="space-y-0.5">
      {shown.map((r) => (
        <AwayRunRow key={r.id} run={r} />
      ))}
      {more > 0 && (
        <Link
          to="/tickets"
          className="flex h-7 items-center gap-1.5 rounded-control px-2 text-xs text-moon-500 hover:bg-ink-800 hover:text-moon-200"
        >
          +{more} more
          <ArrowRight size={12} />
        </Link>
      )}
    </div>
  );
}

/** One finished run since your last visit: outcome dot, TICKET TITLE, cost,
 *  when — a timeline of what finished, not which model ran. The model rides in
 *  a tooltip (the rail is too narrow for it inline; Tooltip no-ops when the
 *  run has none). The whole row links to the run (middle-click opens a tab). */
function AwayRunRow({ run }: { run: RunOut }) {
  const ok = run.exit_status === "success";
  return (
    <Tooltip content={run.model_used} mono>
      <Link
        to="/tickets/$id/runs/$rid"
        params={{ id: run.ticket_id, rid: run.id }}
        className="flex h-7 items-center gap-2 rounded-control px-2 text-xs hover:bg-ink-800"
      >
        <span
          className={cn("h-1.5 w-1.5 shrink-0 rounded-full", ok ? "bg-success" : "bg-failed")}
          aria-hidden
        />
        <span className="min-w-0 flex-1 truncate text-moon-200">
          {run.ticket_title ?? run.model_used ?? "run"}
        </span>
        {run.cost_usd != null && (
          <span className="shrink-0 font-mono tabular-nums text-moon-400">{formatUsd(run.cost_usd)}</span>
        )}
        <span className="shrink-0 text-moon-600">{relativeTime(run.finished_at)}</span>
      </Link>
    </Tooltip>
  );
}
