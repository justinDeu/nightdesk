import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type {
  AgentAnswer,
  AgentCreate,
  AgentDetailOut,
  AgentEnvPut,
  AgentMessage,
  AgentOut,
  AgentPendingItem,
  AgentRestart,
  AgentTurnOut,
} from "./types";

const BASE = "/api/v1/agents";

/** Live transcript SSE endpoint for one agent (same protocol as tickets/runs). */
export const agentTranscriptPath = (id: string) => `${BASE}/${id}/transcript`;

export const agentsApi = {
  list: () => api.get<AgentOut[]>(BASE),
  get: (id: string) => api.get<AgentDetailOut>(`${BASE}/${id}`),
  create: (body: AgentCreate) => api.post<AgentDetailOut>(BASE, { body }),
  remove: (id: string) => api.delete<void>(`${BASE}/${id}`),
  pending: () => api.get<AgentPendingItem[]>(`${BASE}/pending`),

  postMessage: (id: string, body: AgentMessage) =>
    api.post<AgentTurnOut>(`${BASE}/${id}/messages`, { body }),
  interrupt: (id: string) => api.post<AgentTurnOut>(`${BASE}/${id}/interrupt`),
  end: (id: string) => api.post<AgentOut>(`${BASE}/${id}/end`),
  wake: (id: string) => api.post<AgentOut>(`${BASE}/${id}/wake`),
  answer: (id: string, requestId: string, body: AgentAnswer) =>
    api.post<AgentTurnOut>(`${BASE}/${id}/pending/${requestId}`, { body }),
  putEnv: (id: string, body: AgentEnvPut) =>
    api.put<AgentDetailOut>(`${BASE}/${id}/env`, { body }),
  restartRuntime: (id: string, body: AgentRestart) =>
    api.post<AgentTurnOut>(`${BASE}/${id}/restart-runtime`, { body }),
};

// --- Query hooks ---------------------------------------------------------------

export function useAgents(options?: Partial<UseQueryOptions<AgentOut[]>>) {
  return useQuery({
    queryKey: qk.agents.list,
    queryFn: () => agentsApi.list(),
    ...options,
  });
}

export function useAgent(
  id: string | undefined,
  options?: Partial<UseQueryOptions<AgentDetailOut>>,
) {
  return useQuery({
    queryKey: qk.agents.detail(id ?? ""),
    queryFn: () => agentsApi.get(id as string),
    enabled: !!id,
    ...options,
  });
}

/** Open human-input requests across all agents. Feeds the sidebar badge, the
 *  Desk "Agents waiting on you" band, and per-row pending flags. Polls so a
 *  block raised from another surface still lights the badge. */
export function usePendingAgents(pollMs = 15000) {
  return useQuery({
    queryKey: qk.agents.pending,
    queryFn: () => agentsApi.pending(),
    refetchInterval: pollMs,
    retry: false,
    staleTime: 0,
  });
}

export function useCreateAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AgentCreate) => agentsApi.create(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.agents.all }),
  });
}

export function useDeleteAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => agentsApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.agents.all }),
  });
}

/** Shared invalidation after any control action: refresh this agent's detail
 *  (turns / liveness / pending) plus the list and the cross-agent pending feed. */
function useAgentControl<TVars, TData>(
  id: string,
  fn: (vars: TVars) => Promise<TData>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.agents.detail(id) });
      qc.invalidateQueries({ queryKey: qk.agents.list });
      qc.invalidateQueries({ queryKey: qk.agents.pending });
    },
  });
}

export function usePostMessage(id: string) {
  return useAgentControl(id, (body: AgentMessage) => agentsApi.postMessage(id, body));
}

export function useInterrupt(id: string) {
  return useAgentControl(id, () => agentsApi.interrupt(id));
}

export function useEndAgent(id: string) {
  return useAgentControl(id, () => agentsApi.end(id));
}

export function useWake(id: string) {
  return useAgentControl(id, () => agentsApi.wake(id));
}

export function useAnswerPending(id: string) {
  return useAgentControl(
    id,
    (vars: { requestId: string; body: AgentAnswer }) =>
      agentsApi.answer(id, vars.requestId, vars.body),
  );
}

export function usePutEnv(id: string) {
  return useAgentControl(id, (body: AgentEnvPut) => agentsApi.putEnv(id, body));
}

export function useRestartRuntime(id: string) {
  return useAgentControl(id, (body: AgentRestart) => agentsApi.restartRuntime(id, body));
}
