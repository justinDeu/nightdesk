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

// Module-scoped (not per-hook-instance) so it serializes across every caller —
// ConnectionsSection's per-repo-link project dropdown and ProjectsSection's
// per-project repo-link dropdown each call `useToggleProjectRepoLink()`
// independently, and even within one dropdown `keepOpen` lets a user fire
// several toggles for the same project before the first PUT returns. Keyed by
// projectId; holds only the current tail of that project's toggle chain.
const projectRepoLinkQueues = new Map<string, Promise<unknown>>();

/** Run `op` after every previously-queued toggle for `projectId` has settled
 *  (success or failure), so each toggle's "fetch current, then PUT merged"
 *  round-trip sees the previous toggle's write instead of racing it — two
 *  concurrent toggles reading the same pre-toggle list would otherwise
 *  compute conflicting merges and the second PUT would silently drop the
 *  first attachment (lost update). A failed toggle doesn't wedge the queue:
 *  the chain continues via the reject handler. The map entry is removed once
 *  its chain is idle again so it can't grow unbounded over a long session. */
function enqueueProjectRepoLinkToggle<T>(projectId: string, op: () => Promise<T>): Promise<T> {
  const prior = projectRepoLinkQueues.get(projectId) ?? Promise.resolve();
  const next = prior.then(op, op);
  const tracked = next.catch(() => undefined);
  projectRepoLinkQueues.set(projectId, tracked);
  void tracked.finally(() => {
    if (projectRepoLinkQueues.get(projectId) === tracked) projectRepoLinkQueues.delete(projectId);
  });
  return next;
}

/** Attach/detach one repo link for one project. `setProjectRepoLinks` is a PUT
 *  that replaces the *entire* repo_link_ids list for the project, so a single
 *  toggle first re-fetches the project's current links and merges — otherwise
 *  toggling one repo off would clobber every other repo already attached.
 *  Concurrent toggles for the same project are serialized via
 *  `enqueueProjectRepoLinkToggle` — see its comment for why. */
export function useToggleProjectRepoLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      projectId,
      repoLinkId,
      attach,
    }: {
      projectId: string;
      repoLinkId: string;
      attach: boolean;
    }) =>
      enqueueProjectRepoLinkToggle(projectId, async () => {
        const current = await integrationsApi.listProjectRepoLinks(projectId);
        const ids = new Set(current.map((rl) => rl.id));
        if (attach) ids.add(repoLinkId);
        else ids.delete(repoLinkId);
        return integrationsApi.setProjectRepoLinks(projectId, Array.from(ids));
      }),
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
