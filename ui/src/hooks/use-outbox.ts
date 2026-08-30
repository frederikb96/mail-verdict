/** TanStack Query hooks for the outbox (send / draft). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { OutboxCreateRequest, OutboxResponse } from "@/types/api";

export function useOutboxList(params: { account_id?: string; status?: string }) {
  return useQuery<OutboxResponse[]>({
    queryKey: ["outbox", params.account_id, params.status],
    queryFn: () => api.outbox.list(params),
    staleTime: 15_000,
  });
}

/** Sends a message or saves a draft, optionally with attachments. */
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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["outbox"] });
    },
  });
}
