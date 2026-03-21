/**
 * Hook for setting/clearing a folder's unified name.
 *
 * Shared between web and React Native.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

/** Mutation to set or clear a folder's unified name. */
export function useUpdateUnifiedName() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      accountId,
      folderId,
      unifiedName,
    }: {
      accountId: string;
      folderId: string;
      unifiedName: string | null;
    }) => api.unified.setUnifiedName(accountId, folderId, unifiedName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["unified"] });
    },
  });
}
