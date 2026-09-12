/** TanStack Query hooks for the notification centre. */

import {
  type QueryClient,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import { alertKeys } from "@/hooks/use-alerts";
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

// The API's own ceiling (GET .../notifications?limit=..., le=500) -- the
// largest unacknowledged_only list a single account can ever return.
const _MAX_UNACKNOWLEDGED_LIST = 500;

/** Every account's own unacknowledged notifications, merged -- there is
 * no cross-account endpoint, so this fans out one request per account
 * (the same pattern useSearchFolders() already uses for folders) rather
 * than scoping to whichever account the sidebar happens to have
 * selected. A write-failure is account-wide by nature, not folder-
 * scoped, so nothing here narrows further the way useAlerts() narrows by
 * folder -- and not is_active-scoped either: the folder-delete guard
 * this bell is the only warning for checks unacknowledged notifications
 * account-wide, inactive accounts included, so hiding them here would
 * make the badge lie in exactly the direction that matters.
 *
 * The list itself is fetched unacknowledged_only, not the plain recent
 * list filtered client-side afterward -- the previous shape read the
 * server's default page (its 100 most recent, acknowledged or not) and
 * filtered in the browser, so an account with over 100 notifications
 * could have every unacknowledged one sitting past that page: the badge
 * read zero, the "Dismiss all" control (gated on this same list) never
 * appeared, and the guard still refused to delete a folder -- blocked
 * with no control anywhere in the interface able to unblock it. The
 * count show on the badge still comes from unacknowledgedCount, the
 * exact predicate the guard itself evaluates, rather than this list's
 * own length: correct even past _MAX_UNACKNOWLEDGED_LIST, where the list
 * can no longer show every row but is still guaranteed non-empty. */
export function useAllAccountsNotifications() {
  const { data: accounts, isLoading: accountsLoading } = useAccounts();
  const allAccounts = accounts ?? [];

  const listResults = useQueries({
    queries: allAccounts.map((account) => ({
      queryKey: [...notificationKeys.list(account.id), "unacknowledged"],
      queryFn: () =>
        api.notifications.list(account.id, {
          unacknowledged_only: true, limit: _MAX_UNACKNOWLEDGED_LIST,
        }),
      staleTime: 10_000,
    })),
  });
  const countResults = useQueries({
    queries: allAccounts.map((account) => ({
      queryKey: notificationKeys.count(account.id),
      queryFn: () => api.notifications.unacknowledgedCount(account.id),
      staleTime: 10_000,
    })),
  });

  const isLoading = accountsLoading || listResults.some((r) => r.isLoading);
  const notifications = allAccounts.flatMap(
    (_account, i) => listResults[i]?.data ?? [],
  );
  const unacknowledgedCount = countResults.reduce(
    (sum, r) => sum + (r.data?.unacknowledged ?? 0), 0,
  );
  return { notifications, isLoading, unacknowledgedCount };
}

/** An acknowledgement moves the bell's badge as well as the lists
 * (useBellBadge counts write failures). */
function invalidateNotificationsAndBadge(qc: QueryClient) {
  return () => {
    qc.invalidateQueries({ queryKey: ["notifications"] });
    qc.invalidateQueries({ queryKey: alertKeys.count });
  };
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
    onSuccess: invalidateNotificationsAndBadge(qc),
  });
}

export function useAcknowledgeAllNotifications() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (accountId: string) => api.notifications.acknowledgeAll(accountId),
    onSuccess: invalidateNotificationsAndBadge(qc),
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
    onSuccess: invalidateNotificationsAndBadge(qc),
  });
}
