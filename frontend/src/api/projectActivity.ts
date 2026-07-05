import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

/** One recent agent run against a project's tickets
 *  (GET /api/v1/projects/{id}/activity). */
export interface ProjectActivityRow {
  run_id: string;
  ticket_id: string;
  ticket_title: string;
  outcome: string;
  duration_seconds: number | null;
  tokens: number | null;
  started_at: string | null;
}

export const projectActivityApi = {
  forProject: (projectId: string) =>
    api.get<ProjectActivityRow[]>(`/api/v1/projects/${projectId}/activity`),
};

export function useProjectActivity(projectId: string | undefined) {
  return useQuery({
    queryKey: ["project-activity", projectId],
    queryFn: () => projectActivityApi.forProject(projectId as string),
    enabled: !!projectId,
    staleTime: 10_000,
  });
}
