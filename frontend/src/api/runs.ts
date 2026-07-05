import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { RunDiff, RunOut } from "./types";

const BASE = "/api/v1/runs";

export const runsApi = {
  list: (params?: { ticket_id?: string }) => api.get<RunOut[]>(BASE, { query: params }),
  get: (id: string) => api.get<RunOut>(`${BASE}/${id}`),
  /** Structured per-file diff for a run's workspace (JSON). */
  diff: (id: string) => api.get<RunDiff>(`${BASE}/${id}/diff`),
  /** Raw run log text. */
  log: (id: string) => api.get<string>(`${BASE}/${id}/log`),
};

export function useRuns(
  ticketId?: string,
  options?: Partial<UseQueryOptions<RunOut[]>>,
) {
  return useQuery({
    queryKey: qk.runs.list(ticketId),
    queryFn: () => runsApi.list(ticketId ? { ticket_id: ticketId } : undefined),
    ...options,
  });
}

export function useRun(id: string | undefined, options?: Partial<UseQueryOptions<RunOut>>) {
  return useQuery({
    queryKey: qk.runs.detail(id ?? ""),
    queryFn: () => runsApi.get(id as string),
    enabled: !!id,
    ...options,
  });
}
