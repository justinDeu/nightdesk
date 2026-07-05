import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { LabelCreate, LabelOut, LabelUpdate } from "./types";

const BASE = "/api/v1/labels";

export const labelsApi = {
  list: () => api.get<LabelOut[]>(BASE),
  get: (id: string) => api.get<LabelOut>(`${BASE}/${id}`),
  create: (body: LabelCreate) => api.post<LabelOut>(BASE, { body }),
  update: (id: string, body: LabelUpdate) => api.patch<LabelOut>(`${BASE}/${id}`, { body }),
  remove: (id: string) => api.delete<void>(`${BASE}/${id}`),
  /** Replace a ticket's label set (PUT /api/v1/labels/tickets/{ticket_id}). */
  setTicketLabels: (ticketId: string, labelIds: string[]) =>
    api.put<LabelOut[]>(`${BASE}/tickets/${ticketId}`, { body: { label_ids: labelIds } }),
};

export function useLabels() {
  return useQuery({ queryKey: qk.labels, queryFn: labelsApi.list });
}

export function useSaveLabel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id?: string; body: LabelCreate | LabelUpdate }) =>
      id ? labelsApi.update(id, body) : labelsApi.create(body as LabelCreate),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.labels }),
  });
}
