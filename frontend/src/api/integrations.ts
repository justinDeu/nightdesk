import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type {
  ConnectionCreate,
  ConnectionOut,
  ConnectionTestResult,
  ConnectionUpdate,
  ExternalLinkCreate,
  ExternalLinkOut,
  GitLabItem,
  GitLabItemPage,
  ImportTicketRequest,
  ProviderProjectOut,
  RepoLinkCreate,
  RepoLinkOut,
  RepoSuggestOut,
  TicketOut,
} from "./types";

const BASE = "/api/v1";

export const integrationsApi = {
  // connections
  listConnections: () => api.get<ConnectionOut[]>(`${BASE}/connections`),
  createConnection: (body: ConnectionCreate) => api.post<ConnectionOut>(`${BASE}/connections`, { body }),
  updateConnection: (id: string, body: ConnectionUpdate) =>
    api.patch<ConnectionOut>(`${BASE}/connections/${id}`, { body }),
  removeConnection: (id: string) => api.delete<void>(`${BASE}/connections/${id}`),
  testConnection: (id: string) => api.post<ConnectionTestResult>(`${BASE}/connections/${id}/test`, {}),
  connectionProjects: (id: string, search: string) =>
    api.get<ProviderProjectOut[]>(`${BASE}/connections/${id}/projects`, { query: { search } }),

  // repo links
  listRepoLinks: (connectionId?: string) =>
    api.get<RepoLinkOut[]>(`${BASE}/repo-links`, { query: connectionId ? { connection_id: connectionId } : undefined }),
  createRepoLink: (body: RepoLinkCreate) => api.post<RepoLinkOut>(`${BASE}/repo-links`, { body }),
  removeRepoLink: (id: string) => api.delete<void>(`${BASE}/repo-links/${id}`),

  // project attach
  listProjectRepoLinks: (projectId: string) =>
    api.get<RepoLinkOut[]>(`${BASE}/projects/${projectId}/repo-links`),
  setProjectRepoLinks: (projectId: string, repoLinkIds: string[]) =>
    api.put<RepoLinkOut[]>(`${BASE}/projects/${projectId}/repo-links`, { body: { repo_link_ids: repoLinkIds } }),
  repoSuggest: (projectId: string) => api.get<RepoSuggestOut>(`${BASE}/projects/${projectId}/repo-suggest`),

  // browse
  listIssues: (repoLinkId: string, query?: Record<string, unknown>) =>
    api.get<GitLabItemPage>(`${BASE}/repo-links/${repoLinkId}/issues`, { query }),
  getIssue: (repoLinkId: string, iid: string) =>
    api.get<GitLabItem>(`${BASE}/repo-links/${repoLinkId}/issues/${iid}`),
  listMrs: (repoLinkId: string, query?: Record<string, unknown>) =>
    api.get<GitLabItemPage>(`${BASE}/repo-links/${repoLinkId}/merge-requests`, { query }),
  getMr: (repoLinkId: string, iid: string) =>
    api.get<GitLabItem>(`${BASE}/repo-links/${repoLinkId}/merge-requests/${iid}`),

  // external links
  listTicketLinks: (ticketId: string) =>
    api.get<ExternalLinkOut[]>(`${BASE}/tickets/${ticketId}/external-links`),
  createTicketLink: (ticketId: string, body: ExternalLinkCreate) =>
    api.post<ExternalLinkOut>(`${BASE}/tickets/${ticketId}/external-links`, { body }),
  removeTicketLink: (ticketId: string, linkId: string) =>
    api.delete<void>(`${BASE}/tickets/${ticketId}/external-links/${linkId}`),
  refreshExternalLink: (linkId: string) =>
    api.post<ExternalLinkOut>(`${BASE}/external-links/${linkId}/refresh`, {}),

  // import
  importTicket: (repoLinkId: string, body: ImportTicketRequest) =>
    api.post<TicketOut>(`${BASE}/repo-links/${repoLinkId}/import-ticket`, { body }),
};

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

export function useConnections() {
  return useQuery({ queryKey: qk.integrations.connections, queryFn: integrationsApi.listConnections });
}

export function useCreateConnection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ConnectionCreate) => integrationsApi.createConnection(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.integrations.connections }),
  });
}

export function useUpdateConnection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ConnectionUpdate }) =>
      integrationsApi.updateConnection(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.integrations.connections }),
  });
}

export function useDeleteConnection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => integrationsApi.removeConnection(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.integrations.connections }),
  });
}

export function useTestConnection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => integrationsApi.testConnection(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.integrations.connections }),
  });
}

export function useRepoLinks(connectionId?: string) {
  return useQuery({
    queryKey: qk.integrations.repoLinks(connectionId),
    queryFn: () => integrationsApi.listRepoLinks(connectionId),
  });
}

export function useCreateRepoLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RepoLinkCreate) => integrationsApi.createRepoLink(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations", "repo-links"] });
      qc.invalidateQueries({ queryKey: qk.integrations.connections });
    },
  });
}

export function useDeleteRepoLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => integrationsApi.removeRepoLink(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations", "repo-links"] });
      qc.invalidateQueries({ queryKey: qk.integrations.connections });
    },
  });
}

export function useProjectRepoLinks(projectId: string) {
  return useQuery({
    queryKey: qk.integrations.projectRepoLinks(projectId),
    queryFn: () => integrationsApi.listProjectRepoLinks(projectId),
    enabled: !!projectId,
  });
}

export function useRepoSuggest(projectId: string) {
  return useQuery({
    queryKey: qk.integrations.repoSuggest(projectId),
    queryFn: () => integrationsApi.repoSuggest(projectId),
    enabled: !!projectId,
  });
}

/** Attach/detach one repo link for one project. `setProjectRepoLinks` is a PUT
 *  that replaces the *entire* repo_link_ids list for the project, so a single
 *  toggle first re-fetches the project's current links and merges — otherwise
 *  toggling one repo off would clobber every other repo already attached. */
export function useToggleProjectRepoLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      projectId,
      repoLinkId,
      attach,
    }: {
      projectId: string;
      repoLinkId: string;
      attach: boolean;
    }) => {
      const current = await integrationsApi.listProjectRepoLinks(projectId);
      const ids = new Set(current.map((rl) => rl.id));
      if (attach) ids.add(repoLinkId);
      else ids.delete(repoLinkId);
      return integrationsApi.setProjectRepoLinks(projectId, Array.from(ids));
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: qk.integrations.projectRepoLinks(vars.projectId) });
      qc.invalidateQueries({ queryKey: qk.integrations.repoSuggest(vars.projectId) });
      // Unscoped prefix: covers both the per-connection and all-connections
      // repo-link list caches (ConnectionsSection's "N projects" counts).
      qc.invalidateQueries({ queryKey: ["integrations", "repo-links"] });
    },
  });
}

export function useTicketExternalLinks(ticketId: string) {
  return useQuery({
    queryKey: qk.integrations.ticketLinks(ticketId),
    queryFn: () => integrationsApi.listTicketLinks(ticketId),
    enabled: !!ticketId,
  });
}
