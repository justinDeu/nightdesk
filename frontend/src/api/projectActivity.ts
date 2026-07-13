import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api } from "./client";

/**
 * Unified project activity feed (GET /api/v1/projects/{id}/activity) — one
 * merged, reverse-chronological, cursor-paginated stream of run outcomes, ticket
 * lifecycle transitions, repo (MR/issue) events, and cron fires. Filters + search
 * are applied SERVER-SIDE (docs/design/project-control-plane.md §History).
 */

export type ActivityKind = "run" | "lifecycle" | "repo" | "cron";

/** The server-side filter chips. "all" is the unfiltered feed. */
export type ActivityFilter =
  | "all"
  | "runs"
  | "failures"
  | "shipped"
  | "repo"
  | "lifecycle";

export interface ActivityItem {
  id: string;
  kind: ActivityKind;
  ts: string;
  title: string;
  // run-specific
  outcome?: string | null; // success | failed | running | unknown
  duration_seconds?: number | null;
  tokens?: number | null;
  cost_usd?: number | null;
  // deep-links
  run_id?: string | null;
  ticket_id?: string | null;
  // lifecycle-specific
  to_status?: string | null;
  // repo-specific
  repo_kind?: string | null; // merge_request | issue
  repo_link_id?: string | null;
  external_iid?: string | null;
  external_url?: string | null;
  state?: string | null; // opened | merged | closed
  diff_add?: number | null;
  diff_del?: number | null;
  // cron-specific
  skipped_reason?: string | null;
}

export interface WeekRollup {
  week_start: string; // ISO date (Monday)
  runs: number;
  failures: number;
  shipped: number;
  cost_usd: number;
  success_rate: number; // 0..1
}

export interface ActivityFeed {
  items: ActivityItem[];
  rollups: WeekRollup[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface ActivityParams {
  kind?: ActivityFilter;
  q?: string;
  cursor?: string;
  limit?: number;
  include_rollups?: boolean;
}

export const projectActivityApi = {
  feed: (projectId: string, params: ActivityParams = {}) =>
    api.get<ActivityFeed>(`/api/v1/projects/${projectId}/activity`, {
      query: {
        kind: params.kind,
        q: params.q,
        cursor: params.cursor,
        limit: params.limit,
        include_rollups: params.include_rollups,
      },
    }),
};

/** Infinite, cursor-paginated History feed. Each page appends older rows; the
 *  rollups come back only on the first page (server omits them once a cursor is
 *  present). `kind`/`q` reset the query (new first page). */
export function useProjectActivityFeed(
  projectId: string | undefined,
  { kind = "all", q = "" }: { kind?: ActivityFilter; q?: string } = {},
) {
  return useInfiniteQuery({
    queryKey: ["project-activity-feed", projectId, kind, q.trim()],
    queryFn: ({ pageParam }) =>
      projectActivityApi.feed(projectId as string, {
        kind,
        q: q.trim() || undefined,
        cursor: pageParam,
        // Ask for weekly rollups only on the first page.
        include_rollups: pageParam === undefined,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => (last.has_more ? last.next_cursor ?? undefined : undefined),
    enabled: !!projectId,
    staleTime: 15_000,
  });
}

/** Lightweight recent-runs strip (Settings → Projects dialog). Returns only the
 *  run-kind rows from a small first page, keeping the old ActivityStrip working
 *  against the unified endpoint. */
export function useProjectActivity(projectId: string | undefined) {
  const q = useQuery({
    queryKey: ["project-activity-strip", projectId],
    queryFn: () =>
      projectActivityApi.feed(projectId as string, { kind: "runs", limit: 16 }),
    enabled: !!projectId,
    staleTime: 10_000,
  });
  const data = q.data?.items ?? [];
  return { ...q, data };
}
