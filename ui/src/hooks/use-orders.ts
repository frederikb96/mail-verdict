/** TanStack Query hooks for the orders/tickets register -- see
 * components/orders/ for the screens and use-sse.ts for the
 * order.updated live invalidation. */

import { useMemo } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { withFields } from "@/lib/query-result";
import { waitForFile } from "@/lib/order-upload";
import type { OrderDetail, OrderFileStatus, OrderKind, OrderUpdateRequest } from "@/types/api";

export interface OrdersListFilter {
  state: "all" | "open";
  /** Omitted: every kind, `other` included. */
  kind?: OrderKind;
  favorites: boolean;
  /** Already trimmed and debounced by the caller. */
  q: string;
  /** `event` is the tickets timeline: latest event date first, and the
   * whole timeline in one page rather than fifty at a time. */
  sort: "activity" | "event";
}

/** The timeline holds every ticket at once (the API's own ceiling) so its
 * rows never grow above a reader who has scrolled into the past. */
const TIMELINE_PAGE_SIZE = 500;
const LIST_PAGE_SIZE = 50;

export const orderKeys = {
  list: (filter: OrdersListFilter) =>
    ["orders", "list", filter.sort, filter.kind ?? "all", filter.state, filter.favorites, filter.q] as const,
  detail: (id: string) => ["orders", "detail", id] as const,
};

/** Newest activity first (or by event date), one request re-reading the
 * whole loaded window on invalidation -- the same shape every other paged
 * list here uses. */
export function useOrdersList(filter: OrdersListFilter) {
  const { state, kind, favorites, q, sort } = filter;
  const query = useInfiniteQuery({
    queryKey: orderKeys.list({ state, kind, favorites, q, sort }),
    queryFn: ({ pageParam }: { pageParam: string | null }) =>
      api.orders.list({
        state,
        kind,
        sort,
        favorites: favorites || undefined,
        q: q || undefined,
        before: pageParam ?? undefined,
        limit: sort === "event" ? TIMELINE_PAGE_SIZE : LIST_PAGE_SIZE,
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
  return withFields(query, { items });
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

/** How long, and how often, a freshly added file is asked after. */
const FILE_POLL_INTERVAL_MS = 1500;
const FILE_POLL_TIMEOUT_MS = 90_000;

function refreshAfterFile(queryClient: ReturnType<typeof useQueryClient>, status: OrderFileStatus) {
  queryClient.invalidateQueries({ queryKey: ["orders", "list"] });
  if (status.order_id) {
    queryClient.invalidateQueries({ queryKey: orderKeys.detail(status.order_id) });
  }
}

/** Adds one file: stores it, then waits for the file job to place it. The
 * mutation resolves with the final status -- `attached`, `failed`, or still
 * `pending` when the wait ran out (the entry then announces itself over the
 * event stream and the lists refresh on their own). */
export function useUploadOrderFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ file, orderId }: { file: File; orderId?: string }) => {
      const accepted = await api.orders.uploadFile(file, orderId);
      if (accepted.status !== "pending") return accepted;
      return waitForFile(() => api.orders.fileStatus(accepted.file_id), {
        intervalMs: FILE_POLL_INTERVAL_MS,
        timeoutMs: FILE_POLL_TIMEOUT_MS,
        sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      });
    },
    onSuccess: (status) => refreshAfterFile(queryClient, status),
  });
}

export function useDeleteOrderFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ orderId, fileId }: { orderId: string; fileId: string }) =>
      api.orders.deleteFile(orderId, fileId),
    onSettled: (_data, error, { orderId }) => {
      queryClient.invalidateQueries({ queryKey: ["orders", "list"] });
      queryClient.invalidateQueries({ queryKey: orderKeys.detail(orderId) });
      // Deleting an entry's last file deletes the entry: the detail then answers 404.
      if (error instanceof ApiError && error.status === 404) {
        queryClient.removeQueries({ queryKey: orderKeys.detail(orderId) });
      }
    },
  });
}
