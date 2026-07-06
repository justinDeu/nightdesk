import type { ReactNode } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatTokens, formatUsd } from "@/lib/status";
import { cn } from "@/lib/cn";

/**
 * Recharts styled to the dusk-console tokens. No default palette: series are
 * lamp / steel (queued) / violet (review) / sage (success); grid + axes recede
 * to ink/moon; ticks are IBM Plex Mono; the tooltip matches our styled Tooltip
 * primitive. Single-series charts carry no legend (the title names them);
 * multi-series charts always legend + direct-label so identity never rests on
 * color alone.
 */

// dusk-console series tokens (resolved CSS vars work as SVG fills).
export const SERIES = {
  spend: "var(--color-lamp)",
  tokens: "var(--color-queued)",
  latency: "var(--color-review)",
} as const;

// Categorical hues for the token-type composition (input/output/cache).
export const TOKEN_TYPE_COLORS = [
  "var(--color-lamp)",
  "var(--color-queued)",
  "var(--color-review)",
  "var(--color-success)",
] as const;

// Sequential violet ramp for latency percentiles (light p50 → dark p99), so the
// growing magnitude reads as deepening color, not four unrelated hues.
export const LATENCY_RAMP = ["#c6bef4", "#a99fee", "#8f86e8", "#6f66c9"] as const;

const AXIS_TICK = {
  fontFamily: "var(--font-mono)",
  fontSize: 10,
  fill: "var(--color-moon-600)",
} as const;

export function formatSeconds(s: number): string {
  if (!s || s < 0) return "0s";
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  if (m < 60) return `${m}m ${rem}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

const dayLabel = (iso: string) => {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
};

// Compact model label for crowded axes: drop the vendor prefix, strip angle
// brackets, cap length so 6+ grouped categories don't collide.
const shortModel = (m: string) => {
  const s = m.replace(/^claude-/, "").replace(/[<>]/g, "");
  return s.length > 11 ? `${s.slice(0, 10)}…` : s;
};

// --- Card frame ----------------------------------------------------------------

export function ChartCard({
  title,
  hint,
  children,
  className,
}: {
  title: string;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col rounded-card border border-ink-700/50 bg-ink-900 p-4 shadow-[var(--shadow-raised)]",
        className,
      )}
    >
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h3 className="font-display text-sm font-semibold text-moon-100">{title}</h3>
        {hint && <span className="font-mono text-[11px] text-moon-600">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

// --- Shared styled tooltip -----------------------------------------------------

function StyledTooltip({
  active,
  payload,
  label,
  format,
}: {
  active?: boolean;
  payload?: Array<{ name?: string; value?: number; color?: string }>;
  label?: string | number;
  format: (v: number) => string;
}) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-control border border-ink-700 bg-ink-800 px-2.5 py-1.5 shadow-[var(--shadow-pop)]">
      {label != null && label !== "" && (
        <div className="mb-1 text-[11px] text-moon-400">{label}</div>
      )}
      <div className="space-y-0.5">
        {payload.map((p, i) => (
          <div key={i} className="flex items-center gap-2 text-xs">
            {p.color && (
              <span
                className="h-2 w-2 shrink-0 rounded-[2px]"
                style={{ backgroundColor: p.color }}
              />
            )}
            {p.name && <span className="text-moon-400">{p.name}</span>}
            <span className="ml-auto pl-3 font-mono font-medium text-moon-100">
              {format(Number(p.value ?? 0))}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

const gridProps = {
  stroke: "var(--color-ink-700)",
  strokeOpacity: 0.4,
  vertical: false,
} as const;

// --- Spend over time: area (lamp) ----------------------------------------------

export function SpendArea({ data }: { data: { date: string; value: number }[] }) {
  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="spend-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-lamp)" stopOpacity={0.24} />
            <stop offset="100%" stopColor="var(--color-lamp)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid {...gridProps} />
        <XAxis
          dataKey="date"
          tickFormatter={dayLabel}
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={false}
          minTickGap={24}
        />
        <YAxis
          width={52}
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v) => formatUsd(v)}
        />
        <Tooltip
          cursor={{ stroke: "var(--color-ink-700)" }}
          content={<StyledTooltip format={formatUsd} />}
          labelFormatter={(l) => dayLabel(String(l))}
        />
        <Area
          type="monotone"
          dataKey="value"
          name="Spend"
          stroke="var(--color-lamp)"
          strokeWidth={2}
          fill="url(#spend-fill)"
          dot={false}
          activeDot={{ r: 3.5, fill: "var(--color-lamp)", stroke: "none" }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// --- Tokens over time: bars (steel) --------------------------------------------

export function TokensBars({ data }: { data: { date: string; value: number }[] }) {
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid {...gridProps} />
        <XAxis
          dataKey="date"
          tickFormatter={dayLabel}
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={false}
          minTickGap={24}
        />
        <YAxis
          width={54}
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v) => formatTokens(v)}
        />
        <Tooltip
          cursor={{ fill: "var(--color-ink-700)", fillOpacity: 0.25 }}
          content={<StyledTooltip format={formatTokens} />}
          labelFormatter={(l) => dayLabel(String(l))}
        />
        <Bar dataKey="value" name="Tokens" fill={SERIES.tokens} radius={[2, 2, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// --- Runs over time: bars (low-emphasis steel) ---------------------------------

// Runs are the quietest series, but flat neutral gray read as undesigned. Nudge
// toward the steel token hue at reduced emphasis so it belongs to the family
// (steel = tokens) without competing with it.
const RUNS_BAR = "color-mix(in srgb, var(--color-queued) 62%, var(--color-ink-800))";

const formatInt = (v: number) => Math.round(v).toLocaleString();

export function RunsBars({ data }: { data: { date: string; value: number }[] }) {
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid {...gridProps} />
        <XAxis
          dataKey="date"
          tickFormatter={dayLabel}
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={false}
          minTickGap={24}
        />
        <YAxis
          width={36}
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={false}
          allowDecimals={false}
          tickFormatter={formatInt}
        />
        <Tooltip
          cursor={{ fill: "var(--color-ink-700)", fillOpacity: 0.25 }}
          content={<StyledTooltip format={formatInt} />}
          labelFormatter={(l) => dayLabel(String(l))}
        />
        <Bar dataKey="value" name="Runs" fill={RUNS_BAR} radius={[2, 2, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// --- Latency per model: grouped percentile bars (violet ramp) -------------------

export interface LatencyRow {
  model: string;
  p50: number;
  p90: number;
  p95: number;
  p99: number;
}

export function LatencyGroupedBars({ data }: { data: LatencyRow[] }) {
  const keys = ["p50", "p90", "p95", "p99"] as const;
  return (
    <div>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }} barCategoryGap="22%">
          <CartesianGrid {...gridProps} />
          <XAxis
            dataKey="model"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            interval={0}
            tickFormatter={shortModel}
          />
          <YAxis
            width={48}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v) => formatSeconds(v)}
          />
          <Tooltip
            cursor={{ fill: "var(--color-ink-700)", fillOpacity: 0.25 }}
            content={<StyledTooltip format={formatSeconds} />}
          />
          {keys.map((k, i) => (
            <Bar key={k} dataKey={k} name={k} fill={LATENCY_RAMP[i]} radius={[2, 2, 0, 0]} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <Legend items={keys.map((k, i) => ({ label: k, color: LATENCY_RAMP[i] }))} />
    </div>
  );
}

// --- Token composition: horizontal stacked proportion bar ----------------------

export interface TokenSplit {
  input: number;
  output: number;
  cache_write: number;
  cache_read: number;
}

const SPLIT_KEYS: { key: keyof TokenSplit; label: string }[] = [
  { key: "input", label: "Input" },
  { key: "output", label: "Output" },
  { key: "cache_write", label: "Cache write" },
  { key: "cache_read", label: "Cache read" },
];

export function TokenComposition({ split }: { split: TokenSplit }) {
  const row = { name: "tokens", ...split };
  const total = SPLIT_KEYS.reduce((a, s) => a + split[s.key], 0);
  return (
    <div>
      <ResponsiveContainer width="100%" height={64}>
        <BarChart layout="vertical" data={[row]} margin={{ top: 0, right: 8, bottom: 0, left: 0 }}>
          <XAxis type="number" hide />
          <YAxis type="category" dataKey="name" hide />
          <Tooltip cursor={{ fill: "transparent" }} content={<StyledTooltip format={formatTokens} />} />
          {SPLIT_KEYS.map((s, i) => (
            <Bar
              key={s.key}
              dataKey={s.key}
              name={s.label}
              stackId="t"
              fill={TOKEN_TYPE_COLORS[i]}
              radius={
                i === 0 ? [3, 0, 0, 3] : i === SPLIT_KEYS.length - 1 ? [0, 3, 3, 0] : [0, 0, 0, 0]
              }
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <Legend
        items={SPLIT_KEYS.map((s, i) => ({
          label: s.label,
          color: TOKEN_TYPE_COLORS[i],
          value: total > 0 ? `${Math.round((split[s.key] / total) * 100)}%` : "0%",
        }))}
      />
    </div>
  );
}

// --- Shared legend (direct identity, never color-alone) ------------------------

function Legend({ items }: { items: { label: string; color: string; value?: string }[] }) {
  return (
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
      {items.map((it) => (
        <span key={it.label} className="inline-flex items-center gap-1.5 text-[11px] text-moon-400">
          <span className="h-2 w-2 rounded-[2px]" style={{ backgroundColor: it.color }} />
          {it.label}
          {it.value && <span className="font-mono text-moon-600">{it.value}</span>}
        </span>
      ))}
    </div>
  );
}

// --- Breakdown table with inline magnitude bars --------------------------------

export interface BreakdownRow {
  key: string;
  name: string;
  tokens: number;
  cost: number;
  runs: number;
  unpriced?: boolean;
}

export function BreakdownTable({
  rows,
  barColor = "var(--color-lamp)",
  empty,
}: {
  rows: BreakdownRow[];
  barColor?: string;
  empty: string;
}) {
  if (rows.length === 0) {
    return (
      <div className="flex h-[120px] items-center justify-center text-sm text-moon-600">{empty}</div>
    );
  }
  const max = Math.max(1, ...rows.map((r) => r.tokens));
  const sorted = [...rows].sort((a, b) => b.tokens - a.tokens);
  return (
    <div className="space-y-2 py-0.5">
      {sorted.map((r) => (
        <div key={r.key} className="flex items-center gap-3">
          <span className="flex w-40 shrink-0 items-center gap-1.5">
            <span className="truncate font-mono text-[12px] text-moon-100">{r.name}</span>
            {r.unpriced && (
              <span className="shrink-0 rounded-full border border-warn/25 bg-warn/10 px-1 py-px text-[9px] font-semibold uppercase tracking-wide text-warn">
                unpriced
              </span>
            )}
          </span>
          <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-ink-800">
            <div
              className="absolute inset-y-0 left-0 rounded-full"
              style={{ width: `${(r.tokens / max) * 100}%`, backgroundColor: barColor, opacity: 0.7 }}
            />
          </div>
          <span className="w-8 shrink-0 text-right font-mono text-[10px] text-moon-600">{r.runs}</span>
          <span className="w-14 shrink-0 text-right font-mono text-[11px] text-moon-400">
            {formatTokens(r.tokens)}
          </span>
          <span
            className={cn(
              "w-16 shrink-0 text-right font-mono text-[11px]",
              r.unpriced ? "text-moon-600" : "text-moon-100",
            )}
          >
            {r.unpriced ? "—" : formatUsd(r.cost)}
          </span>
        </div>
      ))}
    </div>
  );
}
