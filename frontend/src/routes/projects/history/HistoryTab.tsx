import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { GitMerge, CircleDot, Repeat, Hourglass, Search, X } from "lucide-react";
import {
  useProjectActivityFeed,
  type ActivityFilter,
  type ActivityItem,
  type WeekRollup,
} from "@/api/projectActivity";
import { useProjectRepoLinks } from "@/api/integrations";
import type { ProjectOut, RepoLinkOut } from "@/api/types";
import { ExternalItemPeek } from "@/components/ExternalItemPeek";
import { Tooltip } from "@/ui/Tooltip";
import { Spinner } from "@/ui/Spinner";
import { parseTs, formatDuration } from "@/lib/time";
import { formatUsd } from "@/lib/status";
import { cn } from "@/lib/cn";
import { dayKey, dayLabel } from "../shared";

/**
 * History tab — the project's record (docs/design/project-control-plane.md
 * §History). One reverse-chronological, day-grouped ledger interleaving run
 * outcomes, lifecycle transitions, repo events, and cron fires; numbers-only
 * week rollups; an ember cluster treatment for bad days; server-side filter
 * chips + search; a right-edge mini time rail; and "Load earlier" pagination.
 *
 * Filters + search hit the server (useProjectActivityFeed) — never a client-side
 * filter over the loaded window, which is exactly what keeps the Failures chip
 * honest about rows past the first page.
 */

const FILTERS: { id: ActivityFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "runs", label: "Runs" },
  { id: "failures", label: "Failures" },
  { id: "shipped", label: "Shipped" },
  { id: "repo", label: "Repo" },
  { id: "lifecycle", label: "Lifecycle" },
];

const BAD_DAY_FAILURES = 3;

interface DayGroup {
  key: string; // YYYY-MM-DD (local)
  items: ActivityItem[];
  failures: number;
  weekStart: string; // YYYY-MM-DD Monday (local)
  bad: boolean;
}

/** ISO-week Monday (local zone) for a timestamp, as YYYY-MM-DD. */
function weekStartOf(iso: string): string | null {
  const ms = parseTs(iso);
  if (ms === null) return null;
  const d = new Date(ms);
  // Monday-start ISO week: Sun(0)->back 6, Mon(1)->0, …, Sat(6)->back 5.
  const diff = d.getDay() === 0 ? 6 : d.getDay() - 1;
  d.setDate(d.getDate() - diff);
  d.setHours(0, 0, 0, 0);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function groupByDay(items: ActivityItem[]): DayGroup[] {
  const byDay = new Map<string, DayGroup>();
  for (const it of items) {
    const key = dayKey(it.ts);
    if (!key) continue;
    const ws = weekStartOf(it.ts) ?? key;
    let g = byDay.get(key);
    if (!g) {
      g = { key, items: [], failures: 0, weekStart: ws, bad: false };
      byDay.set(key, g);
    }
    g.items.push(it);
    if (it.kind === "run" && it.outcome === "failed") g.failures += 1;
  }
  for (const g of byDay.values()) g.bad = g.failures >= BAD_DAY_FAILURES;
  // Reverse-chronological by date.
  return [...byDay.values()].sort((a, b) => (a.key < b.key ? 1 : a.key > b.key ? -1 : 0));
}

export function HistoryTab({ project }: { project: ProjectOut }) {
  const [kind, setKind] = useState<ActivityFilter>("all");
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const navigate = useNavigate();

  // Debounce the search box so typing doesn't fire a request per keystroke.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 250);
    return () => clearTimeout(t);
  }, [q]);

  const feed = useProjectActivityFeed(project.id, { kind, q: debouncedQ });
  const repoLinksQ = useProjectRepoLinks(project.id);

  const pages = feed.data?.pages ?? [];
  const items = useMemo(() => pages.flatMap((p) => p.items), [pages]);
  const rollups = pages[0]?.rollups ?? [];
  const days = useMemo(() => groupByDay(items), [items]);
  const rollupByWeek = useMemo(() => {
    const m = new Map<string, WeekRollup>();
    for (const r of rollups) m.set(r.week_start.slice(0, 10), r);
    return m;
  }, [rollups]);

  const [peek, setPeek] = useState<{ repo: RepoLinkOut; kind: "issue" | "merge_request"; iid: string } | null>(null);

  const railWeeks = useMemo(() => {
    // Distinct weeks present (reverse-chron), each with a bad flag.
    const seen: { weekStart: string; bad: boolean }[] = [];
    const idx = new Map<string, number>();
    for (const d of days) {
      let i = idx.get(d.weekStart);
      if (i === undefined) {
        i = seen.push({ weekStart: d.weekStart, bad: d.bad }) - 1;
        idx.set(d.weekStart, i);
      } else if (d.bad) seen[i].bad = true;
    }
    return seen;
  }, [days]);

  const todayWeek = weekStartOf(new Date().toISOString());

  const scrollToDay = (dayKeyStr: string) => {
    const el = document.getElementById(`history-day-${dayKeyStr}`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const openRow = (it: ActivityItem) => {
    if (it.kind === "repo") {
      const repo = (repoLinksQ.data ?? []).find((r) => r.id === it.repo_link_id);
      if (repo && it.repo_kind && it.external_iid) {
        setPeek({ repo, kind: it.repo_kind === "issue" ? "issue" : "merge_request", iid: it.external_iid });
      }
      return;
    }
    if (it.kind === "run" && it.ticket_id && it.run_id) {
      navigate({ to: "/tickets/$id/runs/$rid", params: { id: it.ticket_id, rid: it.run_id } });
      return;
    }
    if (it.ticket_id) {
      navigate({ to: "/tickets/$id", params: { id: it.ticket_id } });
    }
  };

  const isLoadingFirst = feed.isLoading;
  const isFetchingMore = feed.isFetchingNextPage;
  const hasContent = days.length > 0;

  return (
    <div className="flex gap-0 px-4 pb-10 pt-4 sm:px-6">
      {/* Ledger column */}
      <div className="min-w-0 flex-1">
        <FilterBar
          kind={kind}
          onKind={setKind}
          q={q}
          onQ={setQ}
        />

        {isLoadingFirst ? (
          <div className="flex items-center gap-2 py-10 text-sm text-moon-600">
            <Spinner size={14} /> Loading history…
          </div>
        ) : !hasContent ? (
          <EmptyHistory kind={kind} hasQuery={debouncedQ.trim().length > 0} />
        ) : (
          <Ledger
            days={days}
            rollupByWeek={rollupByWeek}
            onOpen={openRow}
            onLoadMore={feed.hasNextPage ? () => feed.fetchNextPage() : undefined}
            loadingMore={isFetchingMore}
          />
        )}
      </div>

      {/* Right-edge mini time rail */}
      {hasContent && railWeeks.length > 1 && (
        <TimeRail
          weeks={railWeeks}
          todayWeek={todayWeek}
          onJump={(weekStart) => {
            const firstDay = days.find((d) => d.weekStart === weekStart);
            if (firstDay) scrollToDay(firstDay.key);
          }}
        />
      )}

      {peek && (
        <ExternalItemPeek
          key={`${peek.repo.id}:${peek.iid}`}
          repo={peek.repo}
          kind={peek.kind}
          iid={peek.iid}
          projectId={project.id}
          onClose={() => setPeek(null)}
        />
      )}
    </div>
  );
}

// --- filter bar -------------------------------------------------------------

function FilterBar({
  kind,
  onKind,
  q,
  onQ,
}: {
  kind: ActivityFilter;
  onKind: (k: ActivityFilter) => void;
  q: string;
  onQ: (s: string) => void;
}) {
  return (
    <div className="mb-3.5 flex flex-wrap items-center gap-2">
      {FILTERS.map((f) => (
        <button
          key={f.id}
          onClick={() => onKind(f.id)}
          aria-pressed={kind === f.id}
          className={cn(
            "rounded-full border px-3 py-1.5 text-xs transition-colors",
            kind === f.id
              ? "border-moon-600 bg-ink-800 text-moon-100"
              : "border-ink-700 bg-ink-900 text-moon-400 hover:text-moon-100",
          )}
        >
          {f.label}
        </button>
      ))}
      <div className="relative ml-auto w-full max-w-[240px]">
        <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-moon-600" />
        <input
          value={q}
          onChange={(e) => onQ(e.target.value)}
          placeholder="Filter history…"
          className={cn(
            "w-full rounded-control border border-ink-700 bg-ink-900 py-1.5 pl-7 pr-7 text-xs text-moon-100",
            "placeholder:text-moon-600 focus:border-moon-600 focus:outline-none",
          )}
        />
        {q && (
          <button
            onClick={() => onQ("")}
            aria-label="Clear search"
            className="absolute right-1.5 top-1/2 -translate-y-1/2 grid h-5 w-5 place-items-center rounded text-moon-600 hover:text-moon-100"
          >
            <X size={12} />
          </button>
        )}
      </div>
    </div>
  );
}

// --- ledger (day groups + week rollups + load more) -------------------------

function Ledger({
  days,
  rollupByWeek,
  onOpen,
  onLoadMore,
  loadingMore,
}: {
  days: DayGroup[];
  rollupByWeek: Map<string, WeekRollup>;
  onOpen: (it: ActivityItem) => void;
  onLoadMore: (() => void) | undefined;
  loadingMore: boolean;
}) {
  // Emit a week rollup when we leave a week (reverse-chron), and for the final
  // week after the last day. Rendered only when the server returned that week.
  const rendered: React.ReactNode[] = [];
  let lastWeek: string | null = null;
  for (const day of days) {
    if (lastWeek !== null && day.weekStart !== lastWeek) {
      const r = rollupByWeek.get(lastWeek);
      if (r) rendered.push(<WeekRollupRow key={`rollup-${lastWeek}`} rollup={r} />);
    }
    rendered.push(
      <DayGroupView key={`day-${day.key}`} day={day} onOpen={onOpen} />,
    );
    lastWeek = day.weekStart;
  }
  if (lastWeek !== null) {
    const r = rollupByWeek.get(lastWeek);
    if (r) rendered.push(<WeekRollupRow key={`rollup-${lastWeek}`} rollup={r} />);
  }

  return (
    <div>
      {rendered}
      {onLoadMore && (
        <button
          onClick={onLoadMore}
          disabled={loadingMore}
          className="mt-4 w-full rounded-control border border-ink-700 bg-ink-900 py-2.5 text-xs text-moon-400 transition-colors hover:text-moon-100 disabled:opacity-60"
        >
          {loadingMore ? "Loading…" : "Load earlier"}
        </button>
      )}
    </div>
  );
}

function WeekRollupRow({ rollup }: { rollup: WeekRollup }) {
  const pct = Math.round(rollup.success_rate * 100);
  return (
    <div className="my-3 flex flex-wrap items-center gap-x-3.5 gap-y-1 rounded-control border border-dashed border-ink-700 bg-ink-900 px-3 py-2 font-mono text-[11.5px] text-moon-600">
      <span className="font-display font-semibold text-moon-400">Week of {shortDate(rollup.week_start)}</span>
      <span><b className="font-semibold text-moon-400">{rollup.runs}</b> runs</span>
      <span><b className="font-semibold text-moon-400">{rollup.shipped}</b> shipped</span>
      <span><b className="font-semibold text-moon-400">{formatUsd(rollup.cost_usd)}</b></span>
      <span><b className="font-semibold text-moon-400">{pct}%</b> success</span>
    </div>
  );
}

function DayGroupView({ day, onOpen }: { day: DayGroup; onOpen: (it: ActivityItem) => void }) {
  const counts = countKinds(day.items);
  const sub = [
    counts.run > 0 && `${counts.run} run${counts.run === 1 ? "" : "s"}`,
    counts.repo > 0 && `${counts.repo} repo`,
    counts.lifecycle > 0 && `${counts.lifecycle} lifecycle`,
    counts.cron > 0 && `${counts.cron} cron`,
  ].filter(Boolean).join(" · ");

  return (
    <div
      id={`history-day-${day.key}`}
      className={cn(
        "mb-1 scroll-mt-1",
        day.bad && "rounded-r-sm border-l-2 border-failed bg-failed/[0.05] pl-2",
      )}
    >
      <div className="sticky top-0 z-[3] flex items-baseline gap-2.5 border-b border-ink-800 bg-ink-950 px-0 py-3 pb-2">
        <span className="font-display text-sm font-bold text-moon-100">{dayLabel(day.key)}</span>
        <TodayBadge dayKey={day.key} />
        {day.bad ? (
          <span className="text-[11.5px] text-failed">
            {day.failures} failed — bad-day cluster
          </span>
        ) : (
          sub && <span className="text-[11.5px] text-moon-600">{sub}</span>
        )}
      </div>
      <div className="mb-1">
        {day.items.map((it) => (
          <ActivityRow key={`${it.kind}-${it.id}`} item={it} onOpen={onOpen} />
        ))}
      </div>
    </div>
  );
}

function TodayBadge({ dayKey: dk }: { dayKey: string }) {
  const today = new Date();
  const tk = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(
    today.getDate(),
  ).padStart(2, "0")}`;
  if (dk !== tk) return null;
  return (
    <span className="rounded bg-lamp px-1.5 py-0.5 text-[10px] font-bold text-[#06231a]">TODAY</span>
  );
}

// --- one ledger row ---------------------------------------------------------

function ActivityRow({ item, onOpen }: { item: ActivityItem; onOpen: (it: ActivityItem) => void }) {
  const time = shortTime(item.ts);
  const { label, tone, glyph } = pillFor(item);
  return (
    <button
      onClick={() => onOpen(item)}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-[6px] px-2 py-1.5 text-left text-[12.5px] transition-colors",
        "hover:bg-ink-900 focus-visible:bg-ink-900 focus-visible:outline-none",
        item.outcome === "failed" && "opacity-90 hover:opacity-100",
      )}
    >
      <span className="w-11 shrink-0 font-mono text-[11px] text-moon-600">{time}</span>
      <Pill tone={tone} glyph={glyph}>{label}</Pill>
      <span className="min-w-0 flex-1 truncate text-moon-100">{rowTitle(item)}</span>
      <span className="flex shrink-0 items-center gap-2.5 font-mono text-[11px] text-moon-600">
        <RowMeta item={item} />
      </span>
    </button>
  );
}

function RowMeta({ item }: { item: ActivityItem }) {
  const bits: React.ReactNode[] = [];
  if (item.kind === "run") {
    if (item.duration_seconds != null)
      bits.push(<span key="d">{formatDuration(item.duration_seconds * 1000)}</span>);
    if (item.tokens != null) bits.push(<span key="t">{Math.round(item.tokens / 1000)}k tok</span>);
    if (item.cost_usd != null) bits.push(<span key="c">{formatUsd(item.cost_usd)}</span>);
  } else if (item.kind === "repo" && (item.diff_add != null || item.diff_del != null)) {
    if (item.diff_add != null) bits.push(<span key="a" className="text-success">+{item.diff_add}</span>);
    if (item.diff_del != null) bits.push(<span key="r" className="text-failed">−{item.diff_del}</span>);
  }
  if (bits.length === 0) return null;
  return (
    <>
      {bits.map((node, i) => (
        <span key={`m${i}`} className="inline-flex items-center gap-2.5">
          {i > 0 && <span className="text-moon-700">·</span>}
          {node}
        </span>
      ))}
    </>
  );
}

function rowTitle(it: ActivityItem): string {
  if (it.kind === "lifecycle") {
    const verb = it.to_status === "archived" ? "archived" : "entered review";
    return `${it.title} — ${verb}`;
  }
  if (it.kind === "repo") {
    const prefix = it.repo_kind === "merge_request" ? "!" : "#";
    const st = it.state ? ` ${it.state}` : "";
    return `${prefix}${it.external_iid ?? ""} “${it.title}”${st}`;
  }
  if (it.kind === "cron") {
    return it.skipped_reason ? `${it.title} — skipped (${it.skipped_reason})` : `${it.title} — fired`;
  }
  return it.title;
}

interface PillSpec {
  label: string;
  tone: string;
  glyph?: React.ReactNode;
}

function pillFor(it: ActivityItem): PillSpec {
  if (it.kind === "run") {
    if (it.outcome === "success")
      return { label: "Success", tone: "text-success bg-success/15" };
    if (it.outcome === "running")
      return { label: "Running", tone: "text-dawn bg-lamp/15" };
    return { label: "Failed", tone: "text-failed bg-failed/15" };
  }
  if (it.kind === "lifecycle") {
    if (it.to_status === "archived")
      return { label: "Archived", tone: "text-queued bg-queued/15" };
    return { label: "Review", tone: "text-review bg-review/15" };
  }
  if (it.kind === "repo") {
    if (it.repo_kind === "issue")
      return { label: "Issue", tone: "text-azure bg-azure/15", glyph: <CircleDot size={11} /> };
    return { label: "MR", tone: "text-azure bg-azure/15", glyph: <GitMerge size={11} /> };
  }
  // cron
  return { label: "Cron", tone: "text-moon-400 bg-ink-800", glyph: <Repeat size={11} /> };
}

function Pill({ tone, glyph, children }: { tone: string; glyph?: React.ReactNode; children: React.ReactNode }) {
  return (
    <span
      className={cn(
        "flex w-[64px] shrink-0 items-center justify-center gap-1 rounded-[5px] px-1.5 py-0.5",
        "text-[10px] font-bold uppercase tracking-[0.03em]",
        tone,
      )}
    >
      {glyph}
      {children}
    </span>
  );
}

// --- mini time rail ---------------------------------------------------------

function TimeRail({
  weeks,
  todayWeek,
  onJump,
}: {
  weeks: { weekStart: string; bad: boolean }[];
  todayWeek: string | null;
  onJump: (weekStart: string) => void;
}) {
  const n = weeks.length;
  return (
    <div className="sticky top-[72px] hidden shrink-0 self-start md:block">
      <Tooltip content="Click a marker to jump to that week">
        <div className="flex w-[74px] flex-col items-end pr-1 text-moon-600">
          <div className="relative h-[min(70vh,560px)] w-px bg-ink-700">
            {weeks.map((w, i) => {
              const top = n <= 1 ? 0 : (i / (n - 1)) * 100;
              const isToday = w.weekStart === todayWeek;
              return (
                <button
                  key={w.weekStart}
                  onClick={() => onJump(w.weekStart)}
                  style={{ top: `${top}%` }}
                  className="absolute right-0 flex -translate-y-1/2 items-center justify-end gap-1.5 pr-3"
                >
                  <span
                    className={cn(
                      "whitespace-nowrap font-mono text-[10.5px]",
                      isToday ? "font-bold text-lamp" : "text-moon-600 hover:text-moon-100",
                    )}
                  >
                    {shortDate(w.weekStart)}
                  </span>
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full",
                      isToday
                        ? "bg-lamp shadow-[0_0_6px_rgba(69,192,132,0.6)]"
                        : w.bad
                          ? "bg-failed"
                          : "bg-ink-700",
                    )}
                  />
                </button>
              );
            })}
          </div>
        </div>
      </Tooltip>
    </div>
  );
}

// --- empty state ------------------------------------------------------------

function EmptyHistory({ kind, hasQuery }: { kind: ActivityFilter; hasQuery: boolean }) {
  const noun = kind === "failures" ? "failures" : kind === "all" ? "activity" : `${kind} events`;
  return (
    <div className="rounded-control border border-ink-800 bg-ink-900 px-4 py-12 text-center">
      <Hourglass size={20} className="mx-auto mb-2 text-moon-600" />
      <p className="text-sm text-moon-100">No {noun} to show</p>
      <p className="mt-1 text-xs text-moon-600">
        {hasQuery
          ? "Try a different search term or clear the filter."
          : "Runs, ticket transitions, repo events, and cron fires will accumulate here."}
      </p>
    </div>
  );
}

// --- small format helpers ---------------------------------------------------

function countKinds(items: ActivityItem[]) {
  const c = { run: 0, lifecycle: 0, repo: 0, cron: 0 };
  for (const it of items) c[it.kind] += 1;
  return c;
}

function shortTime(iso: string): string {
  const ms = parseTs(iso);
  if (ms === null) return "";
  return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
}

function shortDate(iso: string): string {
  // Accepts YYYY-MM-DD or a full ISO.
  const d = iso.length <= 10 ? new Date(`${iso}T00:00:00`) : new Date(parseTs(iso) ?? Date.parse(iso));
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
