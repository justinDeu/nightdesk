import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type {
  AdditionalDirAdd,
  BulkIdsOnly,
  BulkLabelsUpdate,
  BulkPriorityUpdate,
  BulkProfileUpdate,
  BulkProjectUpdate,
  BulkStatusUpdate,
  BulkUpdateResult,
  DependencyCreate,
  DependencyOut,
  TicketClone,
  TicketContinue,
  TicketCreate,
  TicketNewConversation,
  TicketNextRunContext,
  TicketOut,
  TicketPromote,
  TicketReorder,
  TicketRestart,
  TicketResumeOrRetry,
  TicketStatus,
  TicketTransition,
  TicketUpdate,
  SteerMessageCreate,
  SteerMessageOut,
  SteerQueueOut,
} from "./types";

const BASE = "/api/v1/tickets";

export type TicketListParams = {
  status?: TicketStatus;
  profile_id?: string;
  project_id?: string;
  /** Exact priority band (0-4). */
  priority?: number;
  /** Label name (case-insensitive) or id. */
  label?: string;
  /** Latest-run outcome. */
  outcome?: "succeeded" | "failed";
  /** Free-text substring over title + prompt. */
  q?: string;
  limit?: number;
  offset?: number;
  /**
   * Sort key. "board" (position-stable, default) leaves ordering unchanged for
   * the board callers; "recent"/"created"/"priority"/"cost" pair with `order`.
   */
  sort?: "board" | "recent" | "created" | "priority" | "cost";
  /** Direction for every `sort` except "board" (which ignores it). */
  order?: "asc" | "desc";
};

/** One page of a paginated ticket list plus the server's total for the filter. */
export type TicketPage = {
  items: TicketOut[];
  /** X-Total-Count: total rows matching the filter, ignoring limit/offset. */
  total: number;
  /** The offset this page was fetched at (its next-page cursor is offset+len). */
  offset: number;
};

export const ticketsApi = {
  list: (params?: TicketListParams) => api.get<TicketOut[]>(BASE, { query: params }),
  /** A single page plus the X-Total-Count total (for server-side pagination). */
  listPage: async (params: TicketListParams & { offset: number }): Promise<TicketPage> => {
    const { data, headers } = await api.getWithMeta<TicketOut[]>(BASE, { query: params });
    const total = Number(headers.get("X-Total-Count") ?? data.length);
    return { items: data, total, offset: params.offset };
  },
  get: (id: string) => api.get<TicketOut>(`${BASE}/${id}`),
  create: (body: TicketCreate) => api.post<TicketOut>(BASE, { body }),
  update: (id: string, body: TicketUpdate) => api.patch<TicketOut>(`${BASE}/${id}`, { body }),
  remove: (id: string) => api.delete<void>(`${BASE}/${id}`),

  transition: (id: string, body: TicketTransition) =>
    api.post<TicketOut>(`${BASE}/${id}/transition`, { body }),
  reorder: (body: TicketReorder) => api.post<TicketOut[]>(`${BASE}/reorder`, { body }),
  /** Send a DRAFT ticket back to the inbox (draft-only; 409 otherwise). */
  sendToInbox: (id: string) => api.post<TicketOut>(`${BASE}/${id}/send-to-inbox`),

  runNow: (id: string) => api.post<TicketOut>(`${BASE}/${id}/run-now`),
  cancelRunNow: (id: string) => api.post<TicketOut>(`${BASE}/${id}/cancel-run-now`),
  cancel: (id: string) => api.post<TicketOut>(`${BASE}/${id}/cancel`),
  requeue: (id: string) => api.post<TicketOut>(`${BASE}/${id}/requeue`),
  archive: (id: string) => api.post<TicketOut>(`${BASE}/${id}/archive`),
  unarchive: (id: string) => api.post<TicketOut>(`${BASE}/${id}/unarchive`),

  continue: (id: string, body: TicketContinue) =>
    api.post<TicketOut>(`${BASE}/${id}/continue`, { body }),
  newConversation: (id: string, body: TicketNewConversation) =>
    api.post<TicketOut>(`${BASE}/${id}/new-conversation`, { body }),
  resume: (id: string, body: TicketResumeOrRetry) =>
    api.post<TicketOut>(`${BASE}/${id}/resume`, { body }),
  retry: (id: string, body: TicketResumeOrRetry) =>
    api.post<TicketOut>(`${BASE}/${id}/retry`, { body }),
  restart: (id: string, body: TicketRestart) =>
    api.post<TicketOut>(`${BASE}/${id}/restart`, { body }),
  clone: (id: string, body: TicketClone) =>
    api.post<TicketOut>(`${BASE}/${id}/clone`, { body }),
  setNextRunContext: (id: string, body: TicketNextRunContext) =>
    api.post<TicketOut>(`${BASE}/${id}/next-run-context`, { body }),
  mergeNextRunContext: (id: string) =>
    api.post<TicketOut>(`${BASE}/${id}/merge-next-run-context`),
  addAdditionalDir: (id: string, body: AdditionalDirAdd) =>
    api.post<TicketOut>(`${BASE}/${id}/additional-dirs`, { body }),

  // Mid-run steering (live-run follow-up queue).
  steerList: (id: string) => api.get<SteerQueueOut>(`${BASE}/${id}/steer`),
  steerAdd: (id: string, body: SteerMessageCreate) =>
    api.post<SteerMessageOut>(`${BASE}/${id}/steer`, { body }),
  steerEdit: (id: string, mid: string, body: string) =>
    api.patch<SteerMessageOut>(`${BASE}/${id}/steer/${mid}`, { body: { body } }),
  steerReorder: (id: string, orderedIds: string[]) =>
    api.post<SteerQueueOut>(`${BASE}/${id}/steer/reorder`, { body: { ordered_ids: orderedIds } }),
  steerCancel: (id: string, mid: string) =>
    api.delete<void>(`${BASE}/${id}/steer/${mid}`),
  removeAdditionalDir: (id: string, path: string) =>
    api.delete<TicketOut>(`${BASE}/${id}/additional-dirs`, { query: { path } }),

  // Inbox lifecycle (JSON parity — quick-capture is just create with status=inbox)
  promote: (id: string, body: TicketPromote) =>
    api.post<TicketOut>(`${BASE}/${id}/promote`, { body }),
  decline: (id: string) => api.post<TicketOut>(`${BASE}/${id}/decline`),

  setDescription: (id: string, description: string | null) =>
    api.patch<TicketOut>(`${BASE}/${id}/description`, { body: { description } }),
  setPriority: (id: string, priority: number) =>
    api.patch<TicketOut>(`${BASE}/${id}/priority`, { body: { priority } }),
  setStatus: (id: string, status: TicketStatus) =>
    api.patch<TicketOut>(`${BASE}/${id}/status`, { body: { status } }),
  setProject: (id: string, project_id: string | null) =>
    api.patch<TicketOut>(`${BASE}/${id}/project`, { body: { project_id } }),
  setProfile: (id: string, profile_id: string) =>
    api.patch<TicketOut>(`${BASE}/${id}/profile`, { body: { profile_id } }),

  bulkStatus: (body: BulkStatusUpdate) =>
    api.patch<BulkUpdateResult>(`${BASE}/bulk/status`, { body }),
  bulkPriority: (body: BulkPriorityUpdate) =>
    api.patch<BulkUpdateResult>(`${BASE}/bulk/priority`, { body }),
  bulkProject: (body: BulkProjectUpdate) =>
    api.patch<BulkUpdateResult>(`${BASE}/bulk/project`, { body }),
  bulkProfile: (body: BulkProfileUpdate) =>
    api.patch<BulkUpdateResult>(`${BASE}/bulk/profile`, { body }),
  bulkLabels: (body: BulkLabelsUpdate) =>
    api.patch<BulkUpdateResult>(`${BASE}/bulk/labels`, { body }),
  bulkArchive: (body: BulkIdsOnly) =>
    api.post<BulkUpdateResult>(`${BASE}/bulk/archive`, { body }),
  bulkUnarchive: (body: BulkIdsOnly) =>
    api.post<BulkUpdateResult>(`${BASE}/bulk/unarchive`, { body }),

  dependencies: (id: string) => api.get<DependencyOut[]>(`${BASE}/${id}/dependencies`),
  addDependency: (id: string, body: DependencyCreate) =>
    api.post<DependencyOut>(`${BASE}/${id}/dependencies`, { body }),
  removeDependency: (id: string, dependsOnId: string) =>
    api.delete<void>(`${BASE}/${id}/dependencies/${dependsOnId}`),
};

// --- Query hooks ---------------------------------------------------------------

export function useTickets(
  params?: TicketListParams,
  options?: Partial<UseQueryOptions<TicketOut[]>>,
) {
  return useQuery({
    queryKey: qk.tickets.list(params),
    queryFn: () => ticketsApi.list(params),
    ...options,
  });
}

/**
 * Server-side paginated ticket list. Fetches one `pageSize` window at a time
 * (default 50) and exposes `fetchNextPage` for a "Load more" control, so a
 * screen like the Archive never pulls every row up front. `total` (from
 * X-Total-Count) lets the caller show "N of TOTAL".
 */
export function useTicketPages(
  params: Omit<TicketListParams, "limit" | "offset">,
  pageSize = 50,
  options?: { refetchInterval?: number; enabled?: boolean },
) {
  return useInfiniteQuery({
    queryKey: qk.tickets.list({ ...params, pageSize, paged: true }),
    queryFn: ({ pageParam }): Promise<TicketPage> =>
      ticketsApi.listPage({ ...params, limit: pageSize, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (last: TicketPage) => {
      const loaded = last.offset + last.items.length;
      return loaded < last.total ? loaded : undefined;
    },
    ...options,
  });
}

export function useTicket(id: string | undefined, options?: Partial<UseQueryOptions<TicketOut>>) {
  return useQuery({
    queryKey: qk.tickets.detail(id ?? ""),
    queryFn: () => ticketsApi.get(id as string),
    enabled: !!id,
    ...options,
  });
}

export function useTicketDependencies(id: string | undefined) {
  return useQuery({
    queryKey: qk.tickets.dependencies(id ?? ""),
    queryFn: () => ticketsApi.dependencies(id as string),
    enabled: !!id,
  });
}

/** The live-run steer queue (pending + delivering) plus the backend's inject
 *  capability. Only meaningful while the ticket is running; the caller gates on
 *  `enabled`. Poll while running so a delivered chip clears promptly even if the
 *  transcript SSE invalidation is missed. */
export function useSteerQueue(
  id: string | undefined,
  options?: { enabled?: boolean; refetchInterval?: number },
) {
  return useQuery({
    queryKey: qk.tickets.steer(id ?? ""),
    queryFn: () => ticketsApi.steerList(id as string),
    enabled: !!id && (options?.enabled ?? true),
    refetchInterval: options?.refetchInterval,
  });
}

/** Invalidate every ticket query (list + details). */
export function useInvalidateTickets() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: qk.tickets.all });
}

export function useCreateTicket() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: TicketCreate) => ticketsApi.create(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.tickets.all }),
  });
}

export function useUpdateTicket(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: TicketUpdate) => ticketsApi.update(id, body),
    onSuccess: (t) => {
      qc.setQueryData(qk.tickets.detail(id), t);
      qc.invalidateQueries({ queryKey: qk.tickets.all });
    },
  });
}

export function useTransitionTicket() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: TicketTransition }) =>
      ticketsApi.transition(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.tickets.all }),
  });
}
