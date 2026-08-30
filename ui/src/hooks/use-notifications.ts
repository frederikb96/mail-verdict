/** TanStack Query hooks for the notification centre. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
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
