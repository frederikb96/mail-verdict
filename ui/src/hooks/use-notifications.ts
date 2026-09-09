/** TanStack Query hooks for the notification centre. */

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAccounts } from "@/hooks/use-accounts";
import type { NotificationCountResponse, NotificationResponse } from "@/types/api";

export const notificationKeys = {
  list: (accountId: string) => ["notifications", "list", accountId] as const,
  count: (accountId: string) => ["notifications", "count", accountId] as const,
};

export function useNotifications(accountId: string | null) {
  return useQuery<NotificationResponse[]>({
    queryKey: notificationKeys.list(accountId ?? ""),
    queryFn: () => api.notifications.list(accountId!),
    enabled: !!accountId,
    staleTime: 10_000,
  });
}

export function useUnacknowledgedCount(accountId: string | null) {
  return useQuery<NotificationCountResponse>({
    queryKey: notificationKeys.count(accountId ?? ""),
    queryFn: () => api.notifications.unacknowledgedCount(accountId!),
    enabled: !!accountId,
    staleTime: 10_000,
  });
}

/** Every active account's own notification list, merged -- there is no
 * cross-account endpoint, so this fans out one request per account (the
 * same pattern useSearchFolders() already uses for folders) rather than
 * scoping to whichever account the sidebar happens to have selected. A
 * write-failure is account-wide by nature, not folder-scoped, so nothing
 * here narrows further the way useAlerts() narrows by folder. */
export function useAllAccountsNotifications() {
  const { data: accounts, isLoading: accountsLoading } = useAccounts();
  const activeAccounts = (accounts ?? []).filter((a) => a.is_active);

  const results = useQueries({
    queries: activeAccounts.map((account) => ({
      queryKey: notificationKeys.list(account.id),
      queryFn: () => api.notifications.list(account.id),
      staleTime: 10_000,
    })),
  });

  const isLoading = accountsLoading || results.some((r) => r.isLoading);
  const notifications = activeAccounts.flatMap(
    (_account, i) => results[i]?.data ?? [],
  );
  return { notifications, isLoading };
}

export function useAcknowledgeNotification() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      accountId,
      notificationId,
    }: {
      accountId: string;
      notificationId: number;
    }) => api.notifications.acknowledge(accountId, notificationId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
}

export function useAcknowledgeAllNotifications() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (accountId: string) => api.notifications.acknowledgeAll(accountId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
}

/** Dismiss-all across every account carrying an unacknowledged notification
 * -- there is no cross-account endpoint for this either, so it's one
 * ack-all call per account with something to clear. */
export function useAcknowledgeAllNotificationsEverywhere() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (accountIds: string[]) =>
      Promise.all(accountIds.map((id) => api.notifications.acknowledgeAll(id))),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
}
