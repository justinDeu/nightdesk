import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

/** The resolved (profile + project + ticket + overrides) sandbox config for a
 *  ticket. The server composes many layers; the shape is intentionally loose
 *  here and typed precisely where the settings screen consumes it. */
export type EffectiveConfig = Record<string, unknown>;

export const effectiveConfigApi = {
  forTicket: (ticketId: string) =>
    api.get<EffectiveConfig>(`/api/v1/tickets/${ticketId}/effective-config`),
  preview: (body: Record<string, unknown>) =>
    api.post<EffectiveConfig>("/api/v1/effective-config/preview", { body }),
};

export function useEffectiveConfig(ticketId: string | undefined) {
  return useQuery({
    queryKey: ["effective-config", ticketId],
    queryFn: () => effectiveConfigApi.forTicket(ticketId as string),
    enabled: !!ticketId,
  });
}
