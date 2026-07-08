/** Central query-key factory so invalidation stays consistent across hooks. */

export const qk = {
  tickets: {
    all: ["tickets"] as const,
    list: (params?: Record<string, unknown>) => ["tickets", "list", params ?? {}] as const,
    detail: (id: string) => ["tickets", "detail", id] as const,
    dependencies: (id: string) => ["tickets", id, "dependencies"] as const,
    steer: (id: string) => ["tickets", id, "steer"] as const,
  },
  runs: {
    all: ["runs"] as const,
    list: (ticketId?: string) => ["runs", "list", ticketId ?? null] as const,
    detail: (id: string) => ["runs", "detail", id] as const,
  },
  sessions: {
    all: ["sessions"] as const,
    list: ["sessions", "list"] as const,
    detail: (id: string) => ["sessions", "detail", id] as const,
  },
  projects: {
    all: ["projects"] as const,
    detail: (id: string) => ["projects", "detail", id] as const,
  },
  profiles: {
    all: ["profiles"] as const,
    detail: (id: string) => ["profiles", "detail", id] as const,
  },
  providers: {
    all: ["providers"] as const,
    detail: (id: string) => ["providers", "detail", id] as const,
    catalog: ["providers", "catalog"] as const,
  },
  backends: ["backends"] as const,
  labels: ["labels"] as const,
  config: ["config"] as const,
  windows: ["config", "windows"] as const,
  worker: ["worker", "status"] as const,
  views: ["views"] as const,
  cron: {
    all: ["cron"] as const,
    detail: (id: string) => ["cron", "detail", id] as const,
  },
  search: (q: string, projectId?: string | null) => ["search", q, projectId ?? null] as const,
} as const;
