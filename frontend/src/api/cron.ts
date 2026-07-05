import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { CronJobCreate, CronJobOut, CronJobUpdate, TicketOut } from "./types";

const BASE = "/api/v1/cron-jobs";

export const cronApi = {
  list: () => api.get<CronJobOut[]>(BASE),
  get: (id: string) => api.get<CronJobOut>(`${BASE}/${id}`),
  create: (body: CronJobCreate) => api.post<CronJobOut>(BASE, { body }),
  update: (id: string, body: CronJobUpdate) => api.patch<CronJobOut>(`${BASE}/${id}`, { body }),
  remove: (id: string) => api.delete<void>(`${BASE}/${id}`),
  enable: (id: string) => api.post<CronJobOut>(`${BASE}/${id}/enable`),
  disable: (id: string) => api.post<CronJobOut>(`${BASE}/${id}/disable`),
  fireNow: (id: string) => api.post<TicketOut>(`${BASE}/${id}/fire-now`),
  fireNowAndRun: (id: string) => api.post<TicketOut>(`${BASE}/${id}/fire-now-and-run`),
};

export function useCronJobs() {
  return useQuery({ queryKey: qk.cron.all, queryFn: cronApi.list });
}

export function useSaveCronJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id?: string; body: CronJobCreate | CronJobUpdate }) =>
      id ? cronApi.update(id, body) : cronApi.create(body as CronJobCreate),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.cron.all }),
  });
}
