/** TanStack Query hooks for sync status operations. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export type AccountConnectionState = "ok" | "retrying" | "never_connected";

/**
 * PostIMAP retries a failed account unboundedly with exponential backoff,
 * so `accounts.state === "error"` means "having a bad time", not "dead".
 * `last_full_sync` is what separates the two cases worth telling apart:
 * an account that has completed a full sync pass before is being retried
 * and usually self-heals, one that never has is misconfigured and needs
 * the user to fix something. The one place this is decided -- every
 * surface that shows account health reads it from here rather than
 * computing its own "is this account broken".
 */
export function accountConnectionState(
  account: { state: string },
  syncStatus: { last_full_sync: string | null } | undefined,
): AccountConnectionState {
  if (account.state !== "error") return "ok";
  return syncStatus?.last_full_sync != null ? "retrying" : "never_connected";
}

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
