import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        // Never retry auth failures — they redirect to /login instead.
        if (error instanceof ApiError && (error.status === 401 || error.status === 404)) {
          return false;
        }
        return failureCount < 2;
      },
    },
  },
});
