import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { SearchHit } from "./types";

export const searchApi = {
  search: (q: string, params?: { limit?: number; project_id?: string | null }) =>
    api.get<SearchHit[]>("/api/v1/search", {
      query: { q, limit: params?.limit, project_id: params?.project_id ?? undefined },
    }),
};

/** Global search. Empty query short-circuits to no results (matches the API). */
export function useSearch(q: string, projectId?: string | null) {
  const trimmed = q.trim();
  return useQuery({
    queryKey: qk.search(trimmed, projectId),
    queryFn: () => searchApi.search(trimmed, { project_id: projectId }),
    enabled: trimmed.length > 0,
    placeholderData: (prev) => prev,
  });
}
