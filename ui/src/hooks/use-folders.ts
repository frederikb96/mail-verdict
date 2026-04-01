/** TanStack Query hooks for folder operations. */

import { type QueryClient, keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const folderKeys = {
  list: (accountId: string) => ["folders", accountId] as const,
};

export function useFolders(accountId: string | null) {
  return useQuery({
    queryKey: folderKeys.list(accountId!),
    queryFn: () => api.folders.list(accountId!),
    enabled: !!accountId,
    staleTime: 5_000,
    placeholderData: keepPreviousData,
  });
}

/**
 * Invalidate ALL folder-related caches.
 * Must be used everywhere instead of individual invalidations
 * to keep ["folders"] and ["folder-order"] in sync.
 */
export function invalidateAllFolderCaches(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ["folders"] });
  qc.invalidateQueries({ queryKey: ["folder-order"] });
  qc.invalidateQueries({ queryKey: ["unified"] });
}
