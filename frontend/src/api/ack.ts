import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { AckDigest, BulkAckResult, TicketOut } from "./types";

const BASE = "/api/v1/tickets";

export type BulkAckBody =
  | { ticket_ids: string[] }
  | { project_scope: true; project_id: string | null; before?: string };

export const ackApi = {
  digest: (projectId?: string | null) =>
    api.get<AckDigest>(`${BASE}/ack/digest`, {
      query: projectId ? { project_id: projectId } : undefined,
    }),
  count: () => api.get<{ count: number }>(`${BASE}/ack/count`),
  ack: (id: string) => api.post<TicketOut>(`${BASE}/${id}/ack`),
  bulkAck: (body: BulkAckBody) => api.post<BulkAckResult>(`${BASE}/ack`, { body }),
};

/** Unacknowledged archived work, grouped by project then day. */
export function useAckDigest(
  projectId?: string | null,
  options?: { refetchInterval?: number },
) {
  return useQuery({
    queryKey: qk.ack.digest(projectId),
    queryFn: () => ackApi.digest(projectId),
    ...options,
  });
}

/** Cheap unacked count for the Desk band header. */
export function useAckCount(options?: { refetchInterval?: number }) {
  return useQuery({
    queryKey: qk.ack.count,
    queryFn: () => ackApi.count(),
    ...options,
  });
}

/** Invalidate everything the ack state touches (digest, count, ticket lists). */
function useInvalidateAck() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: qk.ack.all });
    qc.invalidateQueries({ queryKey: qk.tickets.all });
  };
}

export function useAcknowledge() {
  const invalidate = useInvalidateAck();
  return useMutation({
    mutationFn: (id: string) => ackApi.ack(id),
    onSuccess: invalidate,
  });
}

export function useBulkAck() {
  const invalidate = useInvalidateAck();
  return useMutation({
    mutationFn: (body: BulkAckBody) => ackApi.bulkAck(body),
    onSuccess: invalidate,
  });
}
