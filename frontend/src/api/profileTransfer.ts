import { api } from "./client";
import type { ProfileOut } from "./types";

/** Result of importing a profile (native export or CC settings.json). */
export interface ProfileImportResult {
  id: string;
  dropped_fields: string[];
}

/** Copy / export / import actions the base api/profiles.ts does not cover. */
export const profileTransferApi = {
  /** Clone a profile server-side; name collisions resolve to "<name> (copy)". */
  copy: (pid: string) => api.post<ProfileOut>(`/api/v1/profiles/${pid}/copy`),

  /** JSON export with secrets redacted. Returned as a plain object to download. */
  export: (pid: string) =>
    api.get<Record<string, unknown>>(`/api/v1/profiles/${pid}/export`),

  /** Re-import a native nightdesk export (secrets must be re-entered later). */
  import: (payload: Record<string, unknown>, name?: string) =>
    api.post<ProfileImportResult>("/api/v1/profiles/import", {
      body: { payload, name: name || null },
    }),

  /** Translate a Claude Code settings.json into a profile. */
  importFromCc: (settings: Record<string, unknown>, name?: string) =>
    api.post<ProfileImportResult>("/api/v1/profiles/import-from-cc", {
      body: { settings, name: name || null },
    }),
};
