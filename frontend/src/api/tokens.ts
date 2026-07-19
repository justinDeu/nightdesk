import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { qk } from "./keys";

const BASE = "/api/v1/tokens";

export interface TokenOut {
  id: string;
  name: string;
  kind: string;
  bundle: string | null;
  scopes: string[];
  scope_data: Record<string, unknown>;
  prefix_hint: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

/** Mint response: the only place a full token value is ever returned. */
export interface TokenMintResult extends TokenOut {
  token: string;
}

export interface TokenMintRequest {
  name: string;
  bundle?: string | null;
  scopes?: string[] | null;
  profile_allowlist?: string[] | null;
  project_allowlist?: string[] | null;
  expires_in_days?: number | null;
}

export interface ScopeInfo {
  name: string;
  description: string;
  human_only: boolean;
}

export interface BundleInfo {
  name: string;
  description: string;
  /** The scope snapshot this preset mints — taken at mint time, not a live link. */
  scopes: string[];
}

export interface TokenCatalog {
  scopes: ScopeInfo[];
  bundles: BundleInfo[];
}

// The catalog gained descriptions (scopes/bundles became objects). Normalize
// so the SPA tolerates BOTH the new shape and the older bare-string shape —
// the frontend deploys the moment dist rebuilds, which can be ahead of the API
// restart that ships the new response. Old shape just renders without
// descriptions until the API catches up.
type RawTokenCatalog = {
  scopes: Array<string | ScopeInfo>;
  human_only?: string[];
  bundles: BundleInfo[] | Record<string, string[]>;
};

function normalizeCatalog(raw: RawTokenCatalog): TokenCatalog {
  const legacyHumanOnly = new Set(raw.human_only ?? []);
  const scopes: ScopeInfo[] = (raw.scopes ?? []).map((s) =>
    typeof s === "string"
      ? { name: s, description: "", human_only: legacyHumanOnly.has(s) }
      : s,
  );
  const bundles: BundleInfo[] = Array.isArray(raw.bundles)
    ? raw.bundles
    : Object.entries(raw.bundles ?? {}).map(([name, scopeList]) => ({
        name,
        description: "",
        scopes: scopeList,
      }));
  return { scopes, bundles };
}

export const tokensApi = {
  list: () => api.get<TokenOut[]>(BASE),
  catalog: () => api.get<RawTokenCatalog>(`${BASE}/catalog`).then(normalizeCatalog),
  mint: (body: TokenMintRequest) => api.post<TokenMintResult>(BASE, { body }),
  revoke: (id: string) => api.post<TokenOut>(`${BASE}/${id}/revoke`),
  remove: (id: string) => api.delete<void>(`${BASE}/${id}`),
};

export function useTokens() {
  return useQuery({ queryKey: qk.tokens.all, queryFn: tokensApi.list });
}

export function useTokenCatalog() {
  return useQuery({ queryKey: qk.tokens.catalog, queryFn: tokensApi.catalog, staleTime: Infinity });
}

export function useRevokeToken() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => tokensApi.revoke(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.tokens.all }),
  });
}

export function useDeleteToken() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => tokensApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.tokens.all }),
  });
}
