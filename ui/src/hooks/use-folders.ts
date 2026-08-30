/** TanStack Query hooks for folder operations. */

import {
  type QueryClient,
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { FolderCreateRequest } from "@/types/api";

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

export function useCreateFolder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      accountId,
      data,
    }: {
      accountId: string;
      data: FolderCreateRequest;
    }) => api.folders.create(accountId, data),
    onSuccess: () => invalidateAllFolderCaches(qc),
  });
}

/** Destroys every message in the folder on the mail server. Irreversible. */
export function useDeleteFolder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (folderId: string) => api.folders.delete(folderId),
    onSuccess: () => invalidateAllFolderCaches(qc),
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
