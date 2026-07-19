import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { api } from "./client";

/**
 * Subscription usage (GET /api/v1/providers/usage) — point-in-time rate-limit
 * windows for subscription/OAuth provider endpoints (Claude subscription,
 * ChatGPT/Codex). Normalized server-side into a vendor-neutral shape so the
 * worker popover and analytics page render identical bars.
 */

export type UsageSeverity = "normal" | "warning" | "critical";

export interface UsageWindow {
  label: string;
  used_percent: number;
  resets_at: string | null;
  severity: UsageSeverity;
}

export interface EndpointUsage {
  provider_id: string;
  provider_name: string;
  endpoint_id: string;
  endpoint_label: string;
  protocol_kind: string;
  plan: string | null;
  windows: UsageWindow[];
  fetched_at: string;
  // Set when the upstream fetch failed; windows may be empty or slightly stale.
  error: string | null;
}

export interface SubscriptionUsage {
  endpoints: EndpointUsage[];
}

const USAGE_URL = "/api/v1/providers/usage";

// Anthropic rate-limits aggressive polling; the backend caches for 180s, so
// there's no point querying faster.
export const USAGE_POLL = 180_000;

export const usageApi = {
  get: () => api.get<SubscriptionUsage>(USAGE_URL),
};

export function useSubscriptionUsage(
  options?: Partial<UseQueryOptions<SubscriptionUsage>>,
) {
  return useQuery({
    queryKey: ["providers", "usage"],
    queryFn: usageApi.get,
    staleTime: USAGE_POLL,
    retry: false,
    ...options,
  });
}
