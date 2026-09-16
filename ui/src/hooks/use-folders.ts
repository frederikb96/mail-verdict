/** TanStack Query hooks for folder operations. */

import {
  type QueryClient,
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useProjectedCounts } from "@/hooks/use-intent-ledger";
import type { FolderCreateRequest } from "@/types/api";

export const folderKeys = {
  list: (accountId: string) => ["folders", accountId] as const,
};

/** An account's folders, their counts including mail actions the server
 * has not counted yet (mail-intents.ts). */
export function useFolders(accountId: string | null) {
  const query = useQuery({
    queryKey: folderKeys.list(accountId!),
    queryFn: () => api.folders.list(accountId!),
    enabled: !!accountId,
    staleTime: 5_000,
    placeholderData: keepPreviousData,
  });
  const data = useProjectedCounts(query.data, (f) => [f.id], query.dataUpdatedAt);
  return { ...query, data };
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
    mutationFn: ({ folderId, messageCount }: { folderId: string; messageCount: number }) =>
      api.folders.delete(folderId, messageCount),
    onSuccess: () => invalidateAllFolderCaches(qc),
  });
}

/**
 * Invalidate ALL folder-related caches -- every count the sidebar and the
 * settings pages show, per account and unified.
 * Must be used everywhere instead of individual invalidations
 * to keep ["folders"] and ["folder-order"] in sync. The unified mail lists
 * are not folder caches and are left to refreshMailViews (use-mails.ts).
 */
export function invalidateAllFolderCaches(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ["folders"] });
  qc.invalidateQueries({ queryKey: ["folder-order"] });
  qc.invalidateQueries({ queryKey: ["unified", "folders"] });
  qc.invalidateQueries({ queryKey: ["unified", "folder-order"] });
}
