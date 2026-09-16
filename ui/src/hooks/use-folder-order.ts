/** TanStack Query hooks for folder ordering and visibility. */

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useProjectedCounts } from "@/hooks/use-intent-ledger";

export const folderOrderKeys = {
  get: (accountId: string) => ["folder-order", accountId] as const,
};

/** An account's folders in display order, counts projected like useFolders'. */
export function useFolderOrder(accountId: string | null) {
  const query = useQuery({
    queryKey: folderOrderKeys.get(accountId!),
    queryFn: () => api.folderManagement.getOrder(accountId!),
    enabled: !!accountId,
    staleTime: 30_000,
  });
  const folders = useProjectedCounts(
    query.data?.folders, (f) => [f.folder_id], query.dataUpdatedAt,
  );
  const data = useMemo(
    () => (query.data && folders !== query.data.folders ? { ...query.data, folders: folders! } : query.data),
    [query.data, folders],
  );
  return { ...query, data };
}

export function useUpdateFolderOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      accountId,
      order,
    }: {
      accountId: string;
      order: string[];
    }) => api.folderManagement.updateOrder(accountId, order),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: folderOrderKeys.get(variables.accountId),
      });
    },
  });
}

export function useToggleFolderVisibility() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      folderId,
      isVisible,
    }: {
      accountId: string;
      folderId: string;
      isVisible: boolean;
    }) => api.folders.updatePrefs(folderId, { is_visible: isVisible }),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: folderOrderKeys.get(variables.accountId),
      });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
    },
  });
}
