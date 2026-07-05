import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { api } from "./client";
import type { InboxCountOut, InboxItemOut } from "./types";

const BASE = "/api/v1/inbox";

export const inboxApi = {
  list: (projectId?: string | null) =>
    api.get<InboxItemOut[]>(BASE, { query: { project_id: projectId ?? undefined } }),
  count: () => api.get<InboxCountOut>(`${BASE}/count`),
};

export function useInbox(
  projectId?: string | null,
  options?: Partial<UseQueryOptions<InboxItemOut[]>>,
) {
  return useQuery({
    queryKey: ["inbox", "list", projectId ?? null],
    queryFn: () => inboxApi.list(projectId),
    ...options,
  });
}

/** Inbox unread count for the nav badge. Polls slowly; degrades to 0 on error. */
export function useInboxCount(pollMs = 15000) {
  return useQuery({
    queryKey: ["inbox", "count"],
    queryFn: inboxApi.count,
    refetchInterval: pollMs,
    retry: false,
    select: (d) => d.count,
  });
}
