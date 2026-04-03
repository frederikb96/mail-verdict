/** TanStack Query hooks for sync status operations. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useSyncStatus(accountId: string | null) {
  return useQuery({
    queryKey: ["sync-status", accountId],
    queryFn: () => api.accounts.syncStatus(accountId!),
    enabled: !!accountId,
    staleTime: 10_000,
    refetchInterval: 30_000,
  });
}

export function useTriggerSync() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.accounts.triggerSync(id),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ["sync-status", id] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
}
