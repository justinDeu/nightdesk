import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type {
  CatalogOfferingOut,
  EndpointCreate,
  EndpointOut,
  EndpointUpdate,
  ProtocolInfoOut,
  ProviderCreate,
  ProviderOut,
  ProviderRotateCredential,
  ProviderRotateResult,
  ProviderUpdate,
} from "./types";

const PROVIDERS_BASE = "/api/v1/providers";
const ENDPOINTS_BASE = "/api/v1/provider-endpoints";

export const providersApi = {
  list: () => api.get<ProviderOut[]>(PROVIDERS_BASE),
  get: (id: string) => api.get<ProviderOut>(`${PROVIDERS_BASE}/${id}`),
  create: (body: ProviderCreate) => api.post<ProviderOut>(PROVIDERS_BASE, { body }),
  update: (id: string, body: ProviderUpdate) => api.patch<ProviderOut>(`${PROVIDERS_BASE}/${id}`, { body }),
  remove: (id: string) => api.delete<void>(`${PROVIDERS_BASE}/${id}`),
  rotateCredential: (id: string, body: ProviderRotateCredential) =>
    api.post<ProviderRotateResult>(`${PROVIDERS_BASE}/${id}/rotate-credential`, { body }),
  catalog: () => api.get<CatalogOfferingOut[]>(`${PROVIDERS_BASE}/catalog`),
  protocols: () => api.get<ProtocolInfoOut[]>(`${PROVIDERS_BASE}/protocols`),

  createEndpoint: (providerId: string, body: EndpointCreate) =>
    api.post<EndpointOut>(`${PROVIDERS_BASE}/${providerId}/endpoints`, { body }),
  updateEndpoint: (id: string, body: EndpointUpdate) =>
    api.patch<EndpointOut>(`${ENDPOINTS_BASE}/${id}`, { body }),
  removeEndpoint: (id: string) => api.delete<void>(`${ENDPOINTS_BASE}/${id}`),
  pullModels: (id: string) => api.post<EndpointOut>(`${ENDPOINTS_BASE}/${id}/pull-models`, {}),
};

export function useProviders() {
  return useQuery({ queryKey: qk.providers.all, queryFn: providersApi.list });
}

export function useProviderCatalog() {
  return useQuery({ queryKey: qk.providers.catalog, queryFn: providersApi.catalog, staleTime: Infinity });
}

export function useProviderProtocols() {
  return useQuery({ queryKey: qk.providers.protocols, queryFn: providersApi.protocols, staleTime: Infinity });
}

export function useCreateProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ProviderCreate) => providersApi.create(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.providers.all }),
  });
}

export function useUpdateProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ProviderUpdate }) => providersApi.update(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.providers.all }),
  });
}

export function useDeleteProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => providersApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.providers.all }),
  });
}

export function useRotateCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ProviderRotateCredential }) =>
      providersApi.rotateCredential(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.providers.all }),
  });
}

export function useCreateEndpoint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ providerId, body }: { providerId: string; body: EndpointCreate }) =>
      providersApi.createEndpoint(providerId, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.providers.all }),
  });
}

export function useUpdateEndpoint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: EndpointUpdate }) => providersApi.updateEndpoint(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.providers.all }),
  });
}

export function useDeleteEndpoint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => providersApi.removeEndpoint(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.providers.all }),
  });
}

export function usePullModels() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => providersApi.pullModels(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.providers.all }),
  });
}
