/** TanStack Query hooks for folder assignment (mapping) operations. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const folderMappingKeys = {
  get: (accountId: string) => ["folder-mapping", accountId] as const,
};

export function useFolderMapping(accountId: string | null) {
  return useQuery({
    queryKey: folderMappingKeys.get(accountId!),
    queryFn: () => api.folderManagement.getMapping(accountId!),
    enabled: !!accountId,
    staleTime: 60_000,
  });
}

export function useAutoDetectMapping() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ accountId }: { accountId: string }) =>
      api.folderManagement.autoDetectMapping(accountId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: folderMappingKeys.get(variables.accountId),
      });
    },
  });
}

export function useUpdateFolderMapping() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      accountId,
      mapping,
    }: {
      accountId: string;
      mapping: Record<string, string | null>;
    }) => api.folderManagement.updateMapping(accountId, mapping),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: folderMappingKeys.get(variables.accountId),
      });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
    },
  });
}
