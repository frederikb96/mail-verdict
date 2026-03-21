/** TanStack Query hooks for IMAP IDLE per-folder configuration. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const idleKeys = {
  list: (accountId: string) => ["idle-folders", accountId] as const,
};

export function useIdleFolders(accountId: string | null) {
  return useQuery({
    queryKey: idleKeys.list(accountId!),
    queryFn: () => api.folderManagement.getIdleFolders(accountId!),
    enabled: !!accountId,
    staleTime: 60_000,
  });
}

export function useToggleIdle() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      accountId,
      folderId,
      enabled,
    }: {
      accountId: string;
      folderId: string;
      enabled: boolean;
    }) => api.folderManagement.toggleIdle(accountId, folderId, enabled),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: idleKeys.list(variables.accountId),
      });
    },
  });
}

export function useValidateIdle() {
  return useMutation({
    mutationFn: ({
      accountId,
      folderId,
    }: {
      accountId: string;
      folderId: string;
    }) => api.folderManagement.validateIdle(accountId, folderId),
  });
}
