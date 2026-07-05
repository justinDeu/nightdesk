import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

/** JSON twin of the HTML /diagnostics page (GET /api/v1/diagnostics). */
export interface Diagnostics {
  nightdesk_version: string;
  python_version: string;
  platform: string;
  kernel: string;
  bwrap_version: string | null;
  cc_check_status: string | null;
  cc_version: string | null;
  cc_binary_path: string | null;
  cc_check_message: string | null;
}

export const diagnosticsApi = {
  get: () => api.get<Diagnostics>("/api/v1/diagnostics"),
};

export function useDiagnostics() {
  return useQuery({
    queryKey: ["diagnostics"],
    queryFn: diagnosticsApi.get,
    staleTime: 15_000,
  });
}
