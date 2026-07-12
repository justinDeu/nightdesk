import { useMemo } from "react";
import type { ReactNode } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { AlertTriangle, BarChart3, Clock, Coins, Database, ListChecks, Zap } from "lucide-react";
import type { AnalyticsSearch } from "@/router";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { ErrorState } from "@/ui/ErrorState";
import { Select } from "@/ui/Select";
import { Tooltip } from "@/ui/Tooltip";
import { cn } from "@/lib/cn";
import { formatTokens, formatUsd } from "@/lib/status";
import {
  isUnpriced,
  useAnalyticsLatency,
  useAnalyticsSpend,
  useAnalyticsSummary,
  type AnalyticsRange,
  type SpendModelRow,
} from "@/api/analytics";
import { useProjects } from "@/api/projects";
import {
  BreakdownTable,
  ChartCard,
  formatSeconds,
  LatencyGroupedBars,
  RunsBars,
  SERIES,
  SpendArea,
  TokenComposition,
  TokensBars,
  type BreakdownRow,
} from "./charts";

const RANGES: { key: AnalyticsRange; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
];

const ALL_PROJECTS = "__all__";

function pct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${Math.round(v * 100)}%`;
}

export function AnalyticsPage() {
  // Filter state lives in the URL (deep-linkable, same as the tickets filter
  // bar). `models` is the visible subset; absent = all models shown.
  const search = useSearch({ from: "/app/analytics" });
  const navigate = useNavigate();

  const range: AnalyticsRange = search.range ?? "7d";
  const projectId = search.project ?? ALL_PROJECTS;
  const selectedModels = search.models; // string[] | undefined (undefined = all)

  const activeProject = search.project ?? null;

  const setSearch = (next: Partial<AnalyticsSearch>) =>
    navigate({ to: "/analytics", search: (prev) => ({ ...prev, ...next }), replace: true });

  // The whole spend response is scoped server-side by range + project_id, so
  // every derived view below is already project-scoped; model selection is the
  // only client-side scoping left.
  const summaryQ = useAnalyticsSummary();
  const spendQ = useAnalyticsSpend(range, activeProject);
  const latencyQ = useAnalyticsLatency(range, activeProject);
  const projectsQ = useProjects();

  const summary = summaryQ.data;
  const spend = spendQ.data;
  const latency = latencyQ.data;

  const isVisible = (m: string) => (selectedModels ? selectedModels.includes(m) : true);

  // Model universe (with pricing state), from the range spend breakdown.
  const allModels = useMemo(() => spend?.by_model ?? [], [spend]);
  const visibleModels = useMemo(
    () => allModels.filter((m) => isVisible(m.model)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [allModels, selectedModels],
  );

  // Toggle a model in/out of the visible subset. When the result is "all
  // models", collapse to undefined so the URL stays canonical.
  function toggleModel(model: string) {
    const names = allModels.map((m) => m.model);
    const current = selectedModels ?? names;
    const nextSet = current.includes(model)
      ? current.filter((m) => m !== model)
      : [...current, model];
    const isAll = nextSet.length === names.length && names.every((n) => nextSet.includes(n));
    setSearch({ models: isAll ? undefined : nextSet });
  }

  // Model-scoped totals — by_model carries cost + token split + run_count, so
  // the whole tile row and most charts scope precisely by model selection.
  const scoped = useMemo(() => {
    const acc = {
      cost: 0,
      tokens: 0,
      runs: 0,
      input: 0,
      output: 0,
      cache_write: 0,
      cache_read: 0,
      unpriced: 0,
      estimatedRuns: 0,
    };
    for (const m of visibleModels) {
      acc.cost += m.cost ?? 0;
      acc.tokens += m.total_tokens;
      acc.runs += m.run_count;
      acc.input += m.input_tokens;
      acc.output += m.output_tokens;
      acc.cache_write += m.cache_write_tokens;
      acc.cache_read += m.cache_read_tokens;
      acc.estimatedRuns += m.estimated_runs ?? 0;
      if (isUnpriced(m)) acc.unpriced += 1;
    }
    const totalTok = acc.input + acc.output + acc.cache_write + acc.cache_read;
    return { ...acc, cacheHit: totalTok ? acc.cache_read / totalTok : 0 };
  }, [visibleModels]);

  const modelSubset = selectedModels != null;

  // Rich daily series (project-scoped server-side). Tokens scope further by
  // model (per-day by_model breakdown); the daily COST has no per-model split,
  // so spend-over-time stays whole-scope and is annotated on a model subset.
  const daily = spend?.daily_series ?? [];
  const tokensSeries = daily.map((d) => ({
    date: d.date,
    value: modelSubset
      ? Object.entries(d.by_model).reduce((a, [m, t]) => a + (isVisible(m) ? t : 0), 0)
      : d.total_tokens,
  }));
  const spendSeries = daily.map((d) => ({ date: d.date, value: d.cost }));
  const runsSeries = daily.map((d) => ({ date: d.date, value: d.run_count }));
  const hasSpendSeries = spendSeries.some((d) => d.value > 0);
  const hasTokensSeries = tokensSeries.some((d) => d.value > 0);
  const hasRunsSeries = runsSeries.some((d) => d.value > 0);
  const totalRuns = runsSeries.reduce((a, d) => a + d.value, 0);

  // Latency per model (grouped percentiles), scoped by model selection.
  const latencyRows = (latency?.latency_by_model ?? [])
    .filter((m) => isVisible(m.model))
    .map((m) => ({
      model: m.model,
      p50: m.median_seconds,
      p90: m.p90_seconds,
      p95: m.p95_seconds,
      p99: m.p99_seconds,
    }));

  // Spend-by-model table.
  const modelRows: BreakdownRow[] = visibleModels.map((m: SpendModelRow) => ({
    key: m.model,
    name: m.model,
    tokens: m.total_tokens,
    cost: m.cost ?? 0,
    runs: m.run_count,
    unpriced: isUnpriced(m),
    estimated: m.estimated,
    estimatedRuns: m.estimated_runs,
  }));

  // Native per-project rollup — range- and project-scoped server-side.
  // Unassigned tickets arrive as project_id null, name "(no project)".
  const projectRows: BreakdownRow[] = (spend?.by_project ?? []).map((p) => ({
    key: p.project_id ?? "__none__",
    name: p.project_name,
    tokens: p.total_tokens,
    cost: p.cost,
    runs: p.run_count,
    unpriced: isUnpriced(p),
    estimated: p.estimated,
    estimatedRuns: p.estimated_runs,
  }));

  // Range-scoped run stats, run-weighted across the project rollup; falls back
  // to the global summary when the rollup is empty.
  const runStats = useMemo(() => {
    const rows = spend?.by_project ?? [];
    const completed = rows.reduce((a, p) => a + p.run_count, 0);
    const failure = rows.reduce((a, p) => a + Math.round((1 - p.success_rate) * p.run_count), 0);
    if (completed > 0) return { completed, failure, rate: (completed - failure) / completed };
    const g = summary?.run_stats;
    return g
      ? { completed: g.completed, failure: g.failure, rate: g.success_rate }
      : { completed: 0, failure: 0, rate: null as number | null };
  }, [spend?.by_project, summary]);

  const isLoading = summaryQ.isLoading && spendQ.isLoading;
  const rangeLabel = RANGES.find((r) => r.key === range)?.label.toLowerCase() ?? "";

  return (
    <Page
      bleed
      title="Analytics"
      subtitle="Spend, tokens, and latency across runs."
      actions={
        <div className="flex items-center rounded-control border border-ink-700 bg-ink-900 p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.key}
              onClick={() => setSearch({ range: r.key === "7d" ? undefined : r.key })}
              aria-pressed={range === r.key}
              className={cn(
                "rounded-[6px] px-3 py-1 text-xs font-medium transition-colors",
                range === r.key ? "bg-ink-800 text-moon-100" : "text-moon-400 hover:text-moon-100",
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      }
    >
      {/* Filter bar */}
      <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-medium uppercase tracking-wide text-moon-600">
            Project
          </span>
          <Select
            value={projectId}
            onChange={(e) =>
              setSearch({
                project: e.target.value === ALL_PROJECTS ? undefined : e.target.value,
              })
            }
            aria-label="Project"
            className="h-8 w-48"
          >
            <option value={ALL_PROJECTS}>All projects</option>
            {(projectsQ.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>

        {allModels.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] font-medium uppercase tracking-wide text-moon-600">
              Models
            </span>
            {allModels.map((m) => {
              const on = isVisible(m.model);
              const unpriced = isUnpriced(m);
              return (
                <button
                  key={m.model}
                  onClick={() => toggleModel(m.model)}
                  aria-pressed={on}
                  className={cn(
                    "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[11px] transition-colors",
                    on
                      ? "border-ink-700 bg-ink-800 text-moon-100 hover:bg-ink-700"
                      : "border-ink-700 bg-transparent text-moon-600 line-through hover:text-moon-400",
                  )}
                >
                  <span translate="no">{m.model}</span>
                  {unpriced && (
                    <Tooltip content="No pricing for this model">
                      <span className="text-warn">
                        *<span className="sr-only"> (no pricing)</span>
                      </span>
                    </Tooltip>
                  )}
                </button>
              );
            })}
            {modelSubset && (
              <button
                onClick={() => setSearch({ models: undefined })}
                className="text-[11px] text-moon-500 underline decoration-dotted hover:text-moon-100"
              >
                reset
              </button>
            )}
          </div>
        )}
      </div>

      {summaryQ.isError || spendQ.isError ? (
        <ErrorState
          title="Could not load analytics"
          description="Spend and summary data failed to load. Try again."
          action={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                summaryQ.refetch();
                spendQ.refetch();
              }}
            >
              Retry
            </Button>
          }
        />
      ) : isLoading ? (
        <div className="py-20 text-center text-sm text-moon-600">Loading analytics…</div>
      ) : (
        <div className="space-y-9">
          {/* ── Costs ─────────────────────────────────────────────── */}
          <Section title="Costs" first>
            {/* Honest-pricing notice lives with the dollars it qualifies. */}
            {scoped.unpriced > 0 && (
              <div
                role="status"
                className="mb-4 flex items-start gap-2 rounded-card border border-warn/25 bg-warn/[0.06] px-3 py-2 text-xs text-moon-100"
              >
                <AlertTriangle size={14} aria-hidden className="mt-0.5 shrink-0 text-warn" />
                <span>
                  <span className="font-semibold">{scoped.unpriced}</span> model
                  {scoped.unpriced === 1 ? "" : "s"} in this range have no pricing — reported cost
                  understates actual spend. Token counts are accurate; dollar figures cover priced
                  models only.
                </span>
              </div>
            )}
            <div className="grid gap-4 xl:grid-cols-3">
              <ChartCard
                title="Spend over time"
                className="xl:col-span-2"
                hint={modelSubset ? "all models" : formatUsd(scoped.cost)}
              >
                {hasSpendSeries ? (
                  <SpendArea data={spendSeries} />
                ) : (
                  <SparseState label="No priced spend in this range" />
                )}
                {modelSubset && (
                  <p className="mt-2 text-[11px] text-moon-600">
                    Daily cost has no per-model split — this series spans all models in scope.
                  </p>
                )}
              </ChartCard>
              <div className="flex flex-col gap-3">
                <StatTile
                  icon={<Coins size={14} />}
                  label={`Spend · ${rangeLabel}`}
                  value={formatUsd(scoped.cost)}
                  sub={scoped.unpriced > 0 ? `${scoped.unpriced} unpriced` : "priced"}
                  warn={scoped.unpriced > 0}
                  estimatedNote={
                    scoped.estimatedRuns > 0
                      ? `Includes ${scoped.estimatedRuns} run${
                          scoped.estimatedRuns === 1 ? "" : "s"
                        } estimated at current prices`
                      : undefined
                  }
                />
                <StatTile
                  icon={<BarChart3 size={14} />}
                  label="Success rate"
                  value={pct(runStats.rate)}
                  sub={
                    runStats.completed > 0
                      ? `${runStats.failure} of ${runStats.completed} failed`
                      : "—"
                  }
                />
              </div>
            </div>
            <div className="mt-4 grid gap-4 lg:grid-cols-2">
              <ChartCard title="Spend by project" hint={`${projectRows.length} projects`}>
                <BreakdownTable
                  rows={projectRows}
                  barColor={SERIES.spend}
                  empty="No runs to attribute in this range"
                />
              </ChartCard>
              <ChartCard title="Spend by model" hint={`${modelRows.length} models`}>
                <BreakdownTable
                  rows={modelRows}
                  barColor={SERIES.tokens}
                  empty="No models in this range"
                />
              </ChartCard>
            </div>
          </Section>

          {/* ── Token usage ──────────────────────────────────────── */}
          <Section title="Token usage">
            <div className="mb-4 grid gap-3 grid-cols-2 sm:grid-cols-3 xl:max-w-2xl">
              <StatTile
                icon={<Zap size={14} />}
                label={`Tokens · ${rangeLabel}`}
                value={formatTokens(scoped.tokens)}
                sub={`${formatTokens(scoped.input + scoped.output)} non-cache`}
              />
              <StatTile
                icon={<Database size={14} />}
                label="Cache hit"
                value={pct(scoped.cacheHit)}
                sub={`${formatTokens(scoped.cache_read)} reads`}
              />
              <StatTile
                icon={<ListChecks size={14} />}
                label={`Runs · ${rangeLabel}`}
                value={String(scoped.runs)}
                sub={modelSubset ? "selected models" : "all models"}
              />
            </div>
            <div className="grid gap-4 xl:grid-cols-3">
              <ChartCard title="Tokens over time" className="xl:col-span-2" hint={formatTokens(scoped.tokens)}>
                {hasTokensSeries ? (
                  <TokensBars data={tokensSeries} />
                ) : (
                  <SparseState label="No token usage in this range" />
                )}
              </ChartCard>
              <ChartCard title="Token composition" hint={formatTokens(scoped.tokens)}>
                {scoped.tokens > 0 ? (
                  <TokenComposition
                    split={{
                      input: scoped.input,
                      output: scoped.output,
                      cache_write: scoped.cache_write,
                      cache_read: scoped.cache_read,
                    }}
                  />
                ) : (
                  <SparseState label="No tokens in this range" />
                )}
              </ChartCard>
              <ChartCard title="Runs over time" className="xl:col-span-3" hint={`${totalRuns} runs`}>
                {hasRunsSeries ? (
                  <RunsBars data={runsSeries} />
                ) : (
                  <SparseState label="No runs in this range" />
                )}
              </ChartCard>
            </div>
          </Section>

          {/* ── Latency ──────────────────────────────────────────── */}
          <Section title="Latency">
            <div className="grid gap-4 xl:grid-cols-3">
              <ChartCard
                title="Latency by model"
                className="xl:col-span-2"
                hint="p50 · p90 · p95 · p99"
              >
                {latencyQ.isLoading ? (
                  <SparseState label="Loading latency…" />
                ) : latencyRows.length > 0 ? (
                  <LatencyGroupedBars data={latencyRows} />
                ) : (
                  <SparseState label="No latency samples in this range" />
                )}
              </ChartCard>
              <div className="flex flex-col gap-3">
                <StatTile
                  icon={<Clock size={14} />}
                  label="Median run"
                  value={summary ? formatSeconds(summary.duration.median_seconds) : "—"}
                  sub="p50 across runs"
                />
                <StatTile
                  icon={<Clock size={14} />}
                  label="Slow tail"
                  value={summary ? formatSeconds(summary.duration.p90_seconds) : "—"}
                  sub="p90 across runs"
                />
              </div>
            </div>
          </Section>
        </div>
      )}
    </Page>
  );
}

/** A titled analytics band. Separation is spacing + a faint top rule, not a box;
 *  the first section drops the rule so it hugs the filter bar. */
function Section({
  title,
  children,
  first,
}: {
  title: string;
  children: ReactNode;
  first?: boolean;
}) {
  return (
    <section className={cn(!first && "border-t border-ink-700/50 pt-8")}>
      <h2 className="mb-4 font-display text-[13px] font-semibold uppercase tracking-[0.12em] text-moon-400">
        {title}
      </h2>
      {children}
    </section>
  );
}

function StatTile({
  icon,
  label,
  value,
  sub,
  warn,
  estimatedNote,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  sub?: string;
  warn?: boolean;
  /** Tooltip text shown on a small "~" marker next to the value when some of
   *  the figure is repriced at current prices rather than exact history. */
  estimatedNote?: string;
}) {
  return (
    <div className="rounded-card border border-ink-700/50 bg-ink-900 px-4 py-3 shadow-[var(--shadow-raised)]">
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-moon-600">
        <span className="text-moon-400" aria-hidden>
          {icon}
        </span>
        {label}
      </div>
      <div className="flex items-baseline gap-1.5">
        <div className="font-display text-2xl font-semibold tabular-nums text-moon-100">{value}</div>
        {estimatedNote && (
          <Tooltip content={estimatedNote}>
            <span className="cursor-default pb-0.5 text-sm font-medium text-moon-600">~</span>
          </Tooltip>
        )}
      </div>
      {sub && (
        <div
          className={cn(
            "mt-0.5 truncate font-mono text-[11px]",
            warn ? "text-warn" : "text-moon-600",
          )}
        >
          {sub}
        </div>
      )}
    </div>
  );
}

function SparseState({ label }: { label: string }) {
  return (
    <div className="flex h-[200px] items-center justify-center rounded-control border border-dashed border-ink-700 bg-ink-950/30 text-sm text-moon-600">
      {label}
    </div>
  );
}
