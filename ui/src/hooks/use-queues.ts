/** TanStack Query hooks for the background queues (pipeline, embeddings). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { QueuePatchRequest } from "@/types/api";

export const queueKeys = {
  all: ["queues"] as const,
};

// No dedicated SSE event announces queue-state changes (only individual run
// completions do), so this polls -- short enough that "is it moving" reads
// as close to live.
const POLL_MS = 5_000;

export function useQueues() {
  return useQuery({
    queryKey: queueKeys.all,
    queryFn: () => api.queues.list(),
    staleTime: 2_000,
    refetchInterval: POLL_MS,
  });
}

export function usePatchQueue() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, data }: { name: string; data: QueuePatchRequest }) =>
      api.queues.patch(name, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all });
    },
  });
}
