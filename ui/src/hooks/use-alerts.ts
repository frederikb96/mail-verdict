/** TanStack Query hooks for the alert bell -- the durable, in-app record
 * of something worth interrupting the reader for (new mail today, a
 * calendar reminder in a later feature). Not account-scoped: an installed
 * application watches every account from one page, the same breadth the
 * SSE stream itself already has. Folder-scoped, though, the same way the
 * SSE and push paths already are -- both hooks read
 * useEffectiveAlertFolderIds themselves so a caller can't forget it and
 * end up disagreeing with the notification it just got. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useEffectiveAlertFolderIds, useMyPushSubscription } from "@/hooks/use-push";
import type {
  AlertBadgeResponse,
  AlertResponse,
  AlertUnseenCountResponse,
} from "@/types/api";

export const alertKeys = {
  list: ["alerts", "list"] as const,
  count: ["alerts", "count"] as const,
};

export function useAlerts(limit = 50, opts?: { unseenOnly?: boolean }) {
  const folderIds = useEffectiveAlertFolderIds();
  const unseenOnly = opts?.unseenOnly ?? false;
  return useQuery<AlertResponse[]>({
    queryKey: [...alertKeys.list, limit, folderIds, unseenOnly],
    queryFn: () => api.alerts.list(limit, folderIds, unseenOnly),
    staleTime: 10_000,
  });
}

export function useUnseenAlertCount() {
  const folderIds = useEffectiveAlertFolderIds();
  return useQuery<AlertUnseenCountResponse>({
    queryKey: [...alertKeys.count, folderIds],
    queryFn: () => api.alerts.unseenCount(folderIds),
    staleTime: 10_000,
  });
}

/** The bell's badge, counted server-side (alerts/badge.py) -- the same
 * number a phone's badge shows. This browser's own subscription scopes
 * it when there is one, its effective folder scope otherwise. Keyed
 * under alertKeys.count, so whatever refreshes the count refreshes this. */
export function useBellBadge() {
  const { subscription } = useMyPushSubscription();
  const folderIds = useEffectiveAlertFolderIds();
  const scope = subscription ? { subscriptionId: subscription.id } : { folderIds };
  return useQuery<AlertBadgeResponse>({
    queryKey: [...alertKeys.count, "badge", scope],
    queryFn: () => api.alerts.badge(scope),
    staleTime: 10_000,
  });
}

function useInvalidateAlerts() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: alertKeys.list });
    qc.invalidateQueries({ queryKey: alertKeys.count });
  };
}

export function useDismissAlert() {
  const invalidate = useInvalidateAlerts();
  return useMutation({
    mutationFn: (alertId: string) => api.alerts.dismiss(alertId),
    onSuccess: invalidate,
  });
}

export function useDismissAllAlerts() {
  const invalidate = useInvalidateAlerts();
  return useMutation({
    mutationFn: (kinds?: string[]) => api.alerts.dismissAll(kinds),
    onSuccess: invalidate,
  });
}
