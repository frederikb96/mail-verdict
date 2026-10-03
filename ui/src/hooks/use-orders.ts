/** TanStack Query hooks for the orders/tickets register -- see
 * components/orders/ for the screens and use-sse.ts for the
 * order.updated live invalidation. */

import { useMemo } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { OrderDetail, OrderUpdateRequest } from "@/types/api";

export interface OrdersListFilter {
  state: "all" | "open";
  favorites: boolean;
  /** Already trimmed and debounced by the caller. */
  q: string;
}

export const orderKeys = {
  list: (filter: OrdersListFilter) =>
    ["orders", "list", filter.state, filter.favorites, filter.q] as const,
  detail: (id: string) => ["orders", "detail", id] as const,
};

/** Newest activity first, one request re-reading the whole loaded window
 * on invalidation -- the same shape every other paged list here uses. */
export function useOrdersList(filter: OrdersListFilter) {
  const { state, favorites, q } = filter;
  const query = useInfiniteQuery({
    queryKey: orderKeys.list({ state, favorites, q }),
    queryFn: ({ pageParam }: { pageParam: string | null }) =>
      api.orders.list({
        state,
        favorites: favorites || undefined,
        q: q || undefined,
        before: pageParam ?? undefined,
        limit: 50,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : undefined),
  });
  // Stable across renders that don't change query.data -- orders-page.tsx
  // depends on this array's identity in an effect, and an unmemoized
  // .flatMap() here (a fresh array every call) turns that into an effect
  // that fires every render (see this repo's own notes on that trap).
  const items = useMemo(
    () => query.data?.pages.flatMap((p) => p.items) ?? [],
    [query.data],
  );
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

/** Favorite, open/closed and sealed -- one PATCH, whichever fields the
 * patch names. The answer is the whole detail, so the open order's pane is
 * updated from it directly; every list is re-read because a flag can move
 * the order in or out of the Open and Favorites views. */
export function useUpdateOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: OrderUpdateRequest }) =>
      api.orders.update(id, patch),
    onSuccess: (detail: OrderDetail, { id }) => {
      queryClient.setQueryData(orderKeys.detail(id), detail);
      queryClient.invalidateQueries({ queryKey: ["orders", "list"] });
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
