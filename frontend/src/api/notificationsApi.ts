import { api } from "./client";

/** Fire a synthetic run-completion payload at a webhook URL to prove wiring.
 *  Server returns 204 on success, 422 on a bad URL, or 5xx if the POST fails. */
export const notificationsApi = {
  test: (url: string) => api.post<void>("/api/v1/notifications/test", { body: { url } }),
};
