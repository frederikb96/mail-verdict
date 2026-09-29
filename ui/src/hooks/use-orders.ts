/** TanStack Query hooks for the orders/tickets register -- see
 * components/orders/ for the screens and use-sse.ts for the
 * order.updated live invalidation. */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { OrderDetail } from "@/types/api";

export const orderKeys = {
  list: (state: "all" | "open") => ["orders", "list", state] as const,
  detail: (id: string) => ["orders", "detail", id] as const,
};

/** Newest activity first, one request re-reading the whole loaded window
 * on invalidation -- the same shape every other paged list here uses. */
export function useOrdersList(state: "all" | "open") {
  const query = useInfiniteQuery({
    queryKey: orderKeys.list(state),
    queryFn: ({ pageParam }: { pageParam: string | null }) =>
      api.orders.list({ state, before: pageParam ?? undefined, limit: 50 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : undefined),
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  return { ...query, items };
}

export function useOrderDetail(id: string | null) {
  return useQuery({
    queryKey: orderKeys.detail(id ?? ""),
    queryFn: () => api.orders.get(id as string),
    enabled: id !== null,
  });
}

export function useDeleteOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.orders.delete(id),
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: ["orders", "list"] });
      queryClient.removeQueries({ queryKey: orderKeys.detail(id) });
    },
  });
}

export function useRewriteOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.orders.rewrite(id),
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: orderKeys.detail(id) });
    },
  });
}

export function useMergeOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, into }: { id: string; into: string }) => api.orders.merge(id, into),
    onSuccess: (detail: OrderDetail, { id }) => {
      queryClient.invalidateQueries({ queryKey: ["orders", "list"] });
      queryClient.removeQueries({ queryKey: orderKeys.detail(id) });
      queryClient.setQueryData(orderKeys.detail(detail.id), detail);
    },
  });
}

export function useDetachMail() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      orderId,
      mailKey,
      moveTo,
    }: {
      orderId: string;
      mailKey: string;
      moveTo?: string | null;
    }) => api.orders.detachMail(orderId, mailKey, moveTo),
    onSuccess: (detail, { orderId, moveTo }) => {
      queryClient.invalidateQueries({ queryKey: ["orders", "list"] });
      if (detail === null) {
        queryClient.removeQueries({ queryKey: orderKeys.detail(orderId) });
      } else {
        queryClient.setQueryData(orderKeys.detail(orderId), detail);
      }
      if (moveTo) queryClient.invalidateQueries({ queryKey: orderKeys.detail(moveTo) });
    },
  });
}

export function useOrderCatchUp() {
  return useMutation({
    mutationFn: (params: { account_id: string; days: number; dry_run: boolean }) =>
      api.orders.catchUp(params),
  });
}
