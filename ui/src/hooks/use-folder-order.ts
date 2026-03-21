/** TanStack Query hooks for folder ordering and visibility. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const folderOrderKeys = {
  get: (accountId: string) => ["folder-order", accountId] as const,
};

export function useFolderOrder(accountId: string | null) {
  return useQuery({
    queryKey: folderOrderKeys.get(accountId!),
    queryFn: () => api.folderManagement.getOrder(accountId!),
    enabled: !!accountId,
    staleTime: 30_000,
  });
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
      accountId,
      folderId,
      isVisible,
    }: {
      accountId: string;
      folderId: string;
      isVisible: boolean;
    }) => api.folderManagement.toggleVisibility(accountId, folderId, isVisible),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: folderOrderKeys.get(variables.accountId),
      });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
    },
  });
}
