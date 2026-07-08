import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type {
  SessionCreate,
  SessionMessage,
  SessionOut,
  SessionPromote,
  TicketOut,
} from "./types";

const BASE = "/api/v1/sessions";

export const sessionsApi = {
  list: () => api.get<SessionOut[]>(BASE),
  get: (id: string) => api.get<SessionOut>(`${BASE}/${id}`),
  create: (body: SessionCreate) => api.post<SessionOut>(BASE, { body }),
  postMessage: (id: string, body: SessionMessage) =>
    api.post<SessionOut>(`${BASE}/${id}/messages`, { body }),
  promote: (id: string, body: SessionPromote) =>
    api.post<TicketOut>(`${BASE}/${id}/promote`, { body }),
  archive: (id: string) => api.post<SessionOut>(`${BASE}/${id}/archive`),
  remove: (id: string) => api.delete<void>(`${BASE}/${id}`),
};

// --- Query hooks ---------------------------------------------------------------

export function useSessions(options?: Partial<UseQueryOptions<SessionOut[]>>) {
  return useQuery({
    queryKey: qk.sessions.list,
    queryFn: () => sessionsApi.list(),
    ...options,
  });
}

export function useSession(
  id: string | undefined,
  options?: Partial<UseQueryOptions<SessionOut>>,
) {
  return useQuery({
    queryKey: qk.sessions.detail(id ?? ""),
    queryFn: () => sessionsApi.get(id as string),
    enabled: !!id,
    ...options,
  });
}

export function useCreateSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SessionCreate) => sessionsApi.create(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.sessions.all }),
  });
}

export function usePostSessionMessage(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SessionMessage) => sessionsApi.postMessage(id, body),
    onSuccess: (s) => {
      qc.setQueryData(qk.sessions.detail(id), s);
      qc.invalidateQueries({ queryKey: qk.sessions.list });
    },
  });
}

export function usePromoteSession(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SessionPromote) => sessionsApi.promote(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.sessions.all });
      qc.invalidateQueries({ queryKey: qk.tickets.all });
    },
  });
}

export function useArchiveSession(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => sessionsApi.archive(id),
    onSuccess: (s) => {
      qc.setQueryData(qk.sessions.detail(id), s);
      qc.invalidateQueries({ queryKey: qk.sessions.list });
    },
  });
}

export function useDeleteSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => sessionsApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.sessions.all }),
  });
}
