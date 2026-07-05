import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type {
  ConfigOut,
  ConfigUpdate,
  ScheduleWindowCreate,
  ScheduleWindowOut,
  ScheduleWindowUpdate,
  ScheduleWindowsReplace,
  WorkerStatusOut,
} from "./types";

const BASE = "/api/v1";

export const configApi = {
  get: () => api.get<ConfigOut>(`${BASE}/config`),
  update: (body: ConfigUpdate) => api.patch<ConfigOut>(`${BASE}/config`, { body }),

  listWindows: () => api.get<ScheduleWindowOut[]>(`${BASE}/config/windows`),
  createWindow: (body: ScheduleWindowCreate) =>
    api.post<ScheduleWindowOut>(`${BASE}/config/windows`, { body }),
  updateWindow: (id: number, body: ScheduleWindowUpdate) =>
    api.patch<ScheduleWindowOut>(`${BASE}/config/windows/${id}`, { body }),
  deleteWindow: (id: number) => api.delete<void>(`${BASE}/config/windows/${id}`),
  replaceWindows: (body: ScheduleWindowsReplace) =>
    api.put<ScheduleWindowOut[]>(`${BASE}/config/windows`, { body }),

  workerStatus: () => api.get<WorkerStatusOut>(`${BASE}/worker/status`),
};

export function useConfig() {
  return useQuery({ queryKey: qk.config, queryFn: configApi.get });
}

export function useUpdateConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ConfigUpdate) => configApi.update(body),
    onSuccess: (cfg) => qc.setQueryData(qk.config, cfg),
  });
}

export function useWindows() {
  return useQuery({ queryKey: qk.windows, queryFn: configApi.listWindows });
}

/**
 * Worker status drives the top-strip pill + spend chip. Polls every 3s like the
 * old UI. Retries are disabled so a downed API degrades to the "unknown" state
 * instead of hammering; the query simply reports `isError`.
 */
export function useWorkerStatus(pollMs = 3000) {
  return useQuery({
    queryKey: qk.worker,
    queryFn: configApi.workerStatus,
    refetchInterval: pollMs,
    retry: false,
    staleTime: 0,
  });
}
