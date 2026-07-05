import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export interface FsSuggestResult {
  prefix: string;
  matches: string[];
}

export const fsApi = {
  /** Directory autocomplete for path inputs (GET /api/v1/fs/suggest). */
  suggest: (prefix: string, limit?: number) =>
    api.get<FsSuggestResult>("/api/v1/fs/suggest", { query: { prefix, limit } }),
};

export function useFsSuggest(prefix: string, enabled = true) {
  return useQuery({
    queryKey: ["fs", "suggest", prefix],
    queryFn: () => fsApi.suggest(prefix),
    enabled: enabled && prefix.length > 0,
    placeholderData: (prev) => prev,
  });
}
