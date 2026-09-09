/** TanStack Query hooks for the alert bell -- the durable, in-app record
 * of something worth interrupting the reader for (new mail today, a
 * calendar reminder in a later feature). Not account-scoped: an installed
 * application watches every account from one page, the same breadth the
 * SSE stream itself already has. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AlertResponse, AlertUnseenCountResponse } from "@/types/api";

export const alertKeys = {
  list: ["alerts", "list"] as const,
  count: ["alerts", "count"] as const,
};

export function useAlerts(limit = 50) {
  return useQuery<AlertResponse[]>({
    queryKey: alertKeys.list,
    queryFn: () => api.alerts.list(limit),
    staleTime: 10_000,
  });
}

export function useUnseenAlertCount() {
  return useQuery<AlertUnseenCountResponse>({
    queryKey: alertKeys.count,
    queryFn: () => api.alerts.unseenCount(),
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
    mutationFn: () => api.alerts.dismissAll(),
    onSuccess: invalidate,
  });
}
