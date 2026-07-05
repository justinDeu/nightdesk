import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "./client";

/**
 * Small helper endpoints under /api/v1/preview/* used by the schedule editor
 * (cron-expression preview) and workspace naming. Kept out of api/cron.ts so
 * the cron module stays a thin CRUD mirror.
 */

export interface CronPreviewResult {
  next_fire_times: string[];
}

export const diagnosticsApi = {
  previewCron: (schedule: string, timezone: string, count = 5) =>
    api.post<CronPreviewResult>("/api/v1/preview/cron", {
      body: { schedule, timezone, count },
    }),
};

/** Live next-fire preview for a cron expression. Disabled for empty schedules;
 *  keeps the last good preview visible while a new one loads (no flicker). */
export function useCronPreview(schedule: string, timezone: string, count = 5) {
  return useQuery({
    queryKey: ["preview", "cron", schedule, timezone, count],
    queryFn: () => diagnosticsApi.previewCron(schedule, timezone, count),
    enabled: schedule.trim().length > 0,
    retry: false,
    placeholderData: keepPreviousData,
  });
}
