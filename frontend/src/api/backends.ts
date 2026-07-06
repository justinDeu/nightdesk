import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";
import type { BackendOut } from "./types";

const BACKENDS_BASE = "/api/v1/backends";

export const backendsApi = {
  list: () => api.get<BackendOut[]>(BACKENDS_BASE),
};

/** The harness capability catalog — backend choice, field-group visibility,
 *  and model slots all render from this instead of a hard-coded list. See
 *  docs/design/providers-and-endpoints.md ("Layer 2: Harnesses"). */
export function useBackends() {
  return useQuery({ queryKey: qk.backends, queryFn: backendsApi.list, staleTime: Infinity });
}
