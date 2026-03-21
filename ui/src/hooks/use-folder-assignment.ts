/** TanStack Query hooks for folder assignment (mapping) operations. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const folderMappingKeys = {
  get: (accountId: string) => ["folder-mapping", accountId] as const,
};

export function useFolderMapping(accountId: string | null) {
  return useQuery({
    queryKey: folderMappingKeys.get(accountId!),
    queryFn: () =>
      api.folders
        .list(accountId!)
        .then(() =>
          fetch(`/api/accounts/${accountId}/folder-mapping`).then((r) =>
            r.json(),
          ),
        ),
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
    }) =>
      fetch(`/api/accounts/${accountId}/folder-mapping`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mapping),
      }).then((r) => r.json()),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: folderMappingKeys.get(variables.accountId),
      });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
    },
  });
}
