import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { SavedViewCreate, SavedViewUpdate } from "./types";

/** Saved view. JSON read + write surface (GET/POST/PATCH/DELETE /api/v1/views,
 *  POST /api/v1/views/reorder). Write endpoints may 404 on older API builds;
 *  callers hide the edit affordances when {@link useViewsWritable} is false. */
export interface SavedView {
  id: string;
  name: string;
  surface: string;
  params: Record<string, unknown>;
  url: string;
}

export const viewsApi = {
  list: () => api.get<SavedView[]>("/api/v1/views"),
  create: (body: SavedViewCreate) => api.post<SavedView>("/api/v1/views", { body }),
  update: (id: string, body: SavedViewUpdate) =>
    api.patch<SavedView>(`/api/v1/views/${id}`, { body }),
  remove: (id: string) => api.delete<void>(`/api/v1/views/${id}`),
  reorder: (ids: string[]) =>
    api.post<SavedView[]>("/api/v1/views/reorder", { body: { view_ids: ids } }),
};

export function useViews() {
  return useQuery({
    queryKey: qk.views,
    queryFn: viewsApi.list,
    retry: false,
    // A 404 on the read endpoint means the API predates saved views: treat as
    // "no views" rather than an error surface.
    throwOnError: false,
  });
}

export function useCreateView() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SavedViewCreate) => viewsApi.create(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.views }),
  });
}

export function useRenameView() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => viewsApi.update(id, { name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.views }),
  });
}

export function useDeleteView() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => viewsApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.views }),
  });
}
