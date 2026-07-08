import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { DiffCommentCreate, DiffCommentOut, RequestChangesResult } from "./types";

const RUNS = "/api/v1/runs";
const COMMENTS = "/api/v1/diff-comments";

export const diffCommentsApi = {
  listForRun: (rid: string) => api.get<DiffCommentOut[]>(`${RUNS}/${rid}/comments`),
  create: (rid: string, payload: DiffCommentCreate) =>
    api.post<DiffCommentOut>(`${RUNS}/${rid}/comments`, { body: payload }),
  edit: (cid: string, body: string) =>
    api.patch<DiffCommentOut>(`${COMMENTS}/${cid}`, { body: { body } }),
  resolve: (cid: string) => api.post<DiffCommentOut>(`${COMMENTS}/${cid}/resolve`),
  unresolve: (cid: string) => api.post<DiffCommentOut>(`${COMMENTS}/${cid}/unresolve`),
  remove: (cid: string) => api.delete<void>(`${COMMENTS}/${cid}`),
  requestChanges: (rid: string) =>
    api.post<RequestChangesResult>(`${RUNS}/${rid}/comments/request-changes`),
};

export function useRunComments(
  rid: string | undefined,
  options?: Partial<UseQueryOptions<DiffCommentOut[]>>,
) {
  return useQuery({
    queryKey: qk.runs.comments(rid ?? ""),
    queryFn: () => diffCommentsApi.listForRun(rid as string),
    enabled: !!rid,
    ...options,
  });
}
