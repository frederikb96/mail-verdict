/** TanStack Query hooks for the outbox (send / draft) and the undo-send
 * staging table sends with a nonzero undo window sit in first. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { OutboxCreateRequest, OutboxResponse, PendingSendResponse } from "@/types/api";

export function useOutboxList(params: { account_id?: string; status?: string }) {
  return useQuery<OutboxResponse[]>({
    queryKey: ["outbox", params.account_id, params.status],
    queryFn: () => api.outbox.list(params),
    staleTime: 15_000,
  });
}

/** Sends a message or saves a draft, optionally with attachments. Resolves
 * to a PendingSendResponse instead of an OutboxResponse when the send is
 * held for its undo window -- see api.outbox.create's own docstring. */
export function useCreateOutbox() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      data,
      attachments,
    }: {
      data: OutboxCreateRequest;
      attachments?: File[];
    }) => api.outbox.create(data, attachments),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["outbox"] });
      if ("send_after" in result) {
        qc.invalidateQueries({ queryKey: ["outbox", "pending"] });
      }
    },
  });
}

/** Sends still inside their undo window -- the undo banner's own list,
 * short-polled only while there is anything to show since a row lives a
 * few seconds at most. */
export function usePendingSends(params: { account_id?: string }) {
  return useQuery<PendingSendResponse[]>({
    queryKey: ["outbox", "pending", params.account_id],
    queryFn: () => api.outbox.listPending(params),
    refetchInterval: (query) => (query.state.data?.length ? 1000 : false),
  });
}

/** Cancels a send still inside its undo window. */
export function useCancelPendingSend() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.outbox.cancelPending(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["outbox", "pending"] });
    },
  });
}
