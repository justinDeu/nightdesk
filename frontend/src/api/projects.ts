import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { ProjectCreate, ProjectOut, ProjectUpdate } from "./types";

const BASE = "/api/v1/projects";

export const projectsApi = {
  list: (archived?: boolean) =>
    api.get<ProjectOut[]>(BASE, { query: archived ? { archived: "true" } : undefined }),
  get: (id: string) => api.get<ProjectOut>(`${BASE}/${id}`),
  create: (body: ProjectCreate) => api.post<ProjectOut>(BASE, { body }),
  update: (id: string, body: ProjectUpdate) => api.patch<ProjectOut>(`${BASE}/${id}`, { body }),
  remove: (id: string) => api.delete<void>(`${BASE}/${id}`),
};

/** Active projects by default. Pass `{ archived: true }` for the archived set
 *  (the projects index collapses those under a separate group). */
export function useProjects(opts?: { archived?: boolean }) {
  const archived = opts?.archived;
  return useQuery({
    queryKey: archived ? (["projects", "archived"] as const) : qk.projects.all,
    queryFn: () => projectsApi.list(archived),
  });
}

/** Id → project map for resolving project chips on cards/rows without a
 *  per-ticket fetch. Returns an empty map while loading. */
export function useProjectMap() {
  const { data } = useProjects();
  const map = new Map<string, ProjectOut>();
  for (const p of data ?? []) map.set(p.id, p);
  return map;
}

export function useProject(id: string | undefined) {
  return useQuery({
    queryKey: qk.projects.detail(id ?? ""),
    queryFn: () => projectsApi.get(id as string),
    enabled: !!id,
  });
}

export function useSaveProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id?: string; body: ProjectCreate | ProjectUpdate }) =>
      id ? projectsApi.update(id, body) : projectsApi.create(body as ProjectCreate),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.projects.all }),
  });
}
