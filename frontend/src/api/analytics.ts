import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { api } from "./client";

/**
 * Analytics JSON surface (GET /api/v1/analytics/*). Types are defined locally
 * here — they mirror the aggregates that domain/analytics.py|latency.py|cost.py
 * render, discovered by probing the live API. The SPA charts derive their own
 * single-hue palette from the dusk-console tokens rather than the per-model
 * colors the API returns (those belong to the old HTMX theme).
 */

export type AnalyticsRange = "today" | "7d" | "30d";

export const RANGE_LABEL: Record<AnalyticsRange, string> = {
  today: "Today",
  "7d": "7 days",
  "30d": "30 days",
};

export interface TokenBucket {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  cache_hit_rate: number;
  run_count: number;
  cost: number;
}

export interface AnalyticsSummary {
  price_source_label: string;
  today: TokenBucket;
  last_7d: TokenBucket;
  last_30d: TokenBucket;
  run_stats: {
    completed: number;
    success: number;
    failure: number;
    success_rate: number;
  };
  duration: {
    median_seconds: number;
    p90_seconds: number;
    count: number;
  };
  by_project?: ProjectRollupRow[];
}

/** Per-day aggregate (spend.daily_series and tokens.daily_series share this
 *  shape). Carries the full token split + cost + run_count + per-model tokens,
 *  so one series powers spend-over-time, tokens-over-time, cache-vs-io, and
 *  runs-per-day. */
export interface DailyPoint {
  date: string;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost: number;
  run_count: number;
  by_model: Record<string, number>;
}

/** Native per-project rollup (summary.by_project and spend.by_project).
 *  Unassigned tickets arrive as project_id null, name "(no project)". */
export interface ProjectRollupRow {
  project_id: string | null;
  project_name: string;
  project_slug: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  cache_hit_rate: number;
  run_count: number;
  cost: number;
  success_rate: number;
}

export interface TokensResponse {
  range: string;
  daily_series: DailyPoint[];
  max_daily_tokens: number;
}

export interface LatencyDailyPoint {
  date: string;
  by_model: Record<string, number>;
}

export interface LatencyByModel {
  model: string;
  median_seconds: number;
  p90_seconds: number;
  p95_seconds: number;
  p99_seconds: number;
  count: number;
}

export interface LatencyModelVsTool {
  model: string;
  model_seconds: number;
  tool_seconds: number;
  run_count: number;
}

export interface LatencyResponse {
  range: string;
  latency_series: LatencyDailyPoint[];
  max_daily_latency: number;
  latency_by_model: LatencyByModel[];
  model_vs_tool_time: LatencyModelVsTool[];
}

export interface SpendModelRow {
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  cache_hit_rate: number;
  run_count: number;
  cost: number;
}
export interface SpendProfileRow {
  name: string;
  total_tokens: number;
  run_count: number;
  cost: number;
}
export interface SpendTicketRow {
  ticket_id: string;
  title: string;
  total_tokens: number;
  run_count: number;
  cost: number;
}
export interface SpendResponse {
  range: string;
  project_id: string | null;
  totals: TokenBucket;
  by_model: SpendModelRow[];
  by_profile: SpendProfileRow[];
  by_ticket: SpendTicketRow[];
  by_project: ProjectRollupRow[];
  daily_series: DailyPoint[];
}

const BASE = "/api/v1/analytics";

// Analytics API v2 ships server-side project scoping (project_id on all four
// endpoints, 404 on unknown) plus native per-project rollups and rich daily
// series. When true, `projectId` is sent server-side and the whole response —
// totals, daily series, by_model, latency — is already scoped.
export const ANALYTICS_SUPPORTS_PROJECT_FILTER = true;

function scopeQuery(range: AnalyticsRange, projectId?: string | null) {
  const q: Record<string, string> = { range };
  if (ANALYTICS_SUPPORTS_PROJECT_FILTER && projectId) q.project_id = projectId;
  return q;
}

export const analyticsApi = {
  summary: () => api.get<AnalyticsSummary>(`${BASE}/summary`),
  spend: (range: AnalyticsRange, projectId?: string | null) =>
    api.get<SpendResponse>(`${BASE}/spend`, { query: scopeQuery(range, projectId) }),
  tokens: (range: AnalyticsRange, projectId?: string | null) =>
    api.get<TokensResponse>(`${BASE}/tokens`, { query: scopeQuery(range, projectId) }),
  latency: (range: AnalyticsRange, projectId?: string | null) =>
    api.get<LatencyResponse>(`${BASE}/latency`, { query: scopeQuery(range, projectId) }),
};

/**
 * A model is "unpriced" when it burned tokens but reports no cost — nightdesk's
 * cost engine returns None for models it has no price for (e.g. GLM), which the
 * API currently surfaces as cost 0. Until the API ships an explicit flag, treat
 * tokens-without-cost as unpriced so the UI stops implying real spend was $0.
 */
export function isUnpriced(row: { total_tokens: number; cost: number | null }): boolean {
  return row.total_tokens > 0 && (row.cost == null || row.cost === 0);
}

// Spend polls slowly (mirrors the old UI's 30s spend cadence); the rest are
// on-demand per range.
const SPEND_POLL = 30000;

export function useAnalyticsSummary(options?: Partial<UseQueryOptions<AnalyticsSummary>>) {
  return useQuery({
    queryKey: ["analytics", "summary"],
    queryFn: analyticsApi.summary,
    refetchInterval: SPEND_POLL,
    ...options,
  });
}

// `projectId` is plumbed through now so the whole page rewires to server-side
// project scoping the moment the API supports it (see ANALYTICS_SUPPORTS_PROJECT_FILTER).
export function useAnalyticsSpend(range: AnalyticsRange, projectId?: string | null) {
  return useQuery({
    queryKey: ["analytics", "spend", range, projectId ?? null],
    queryFn: () => analyticsApi.spend(range, projectId),
    refetchInterval: SPEND_POLL,
  });
}

export function useAnalyticsTokens(range: AnalyticsRange, projectId?: string | null) {
  return useQuery({
    queryKey: ["analytics", "tokens", range, projectId ?? null],
    queryFn: () => analyticsApi.tokens(range, projectId),
    refetchInterval: SPEND_POLL,
  });
}

export function useAnalyticsLatency(range: AnalyticsRange, projectId?: string | null) {
  return useQuery({
    queryKey: ["analytics", "latency", range, projectId ?? null],
    queryFn: () => analyticsApi.latency(range, projectId),
    refetchInterval: SPEND_POLL,
  });
}
