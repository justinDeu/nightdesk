import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { ProfileCreate, ProfileOut, ProfileUpdate } from "./types";

const BASE = "/api/v1/profiles";

export const profilesApi = {
  list: () => api.get<ProfileOut[]>(BASE),
  get: (id: string) => api.get<ProfileOut>(`${BASE}/${id}`),
  create: (body: ProfileCreate) => api.post<ProfileOut>(BASE, { body }),
  update: (id: string, body: ProfileUpdate) => api.patch<ProfileOut>(`${BASE}/${id}`, { body }),
  remove: (id: string) => api.delete<void>(`${BASE}/${id}`),
};

export function useProfiles() {
  return useQuery({ queryKey: qk.profiles.all, queryFn: profilesApi.list });
}

export function useProfile(id: string | undefined) {
  return useQuery({
    queryKey: qk.profiles.detail(id ?? ""),
    queryFn: () => profilesApi.get(id as string),
    enabled: !!id,
  });
}

export function useSaveProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id?: string; body: ProfileCreate | ProfileUpdate }) =>
      id ? profilesApi.update(id, body) : profilesApi.create(body as ProfileCreate),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.profiles.all }),
  });
}
