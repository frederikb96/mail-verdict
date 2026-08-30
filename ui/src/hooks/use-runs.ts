/** TanStack Query hooks for pipeline runs -- the "why did this message get
 * that treatment" surface, and the failure store. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const runKeys = {
  list: (status?: string) => ["runs", "list", status ?? "all"] as const,
  detail: (id: string) => ["runs", "detail", id] as const,
  forMail: (mailId: string) => ["runs", "mail", mailId] as const,
};

// pipeline.run_finished invalidates this on every terminal run; the poll is
// only a safety net for a missed or coalesced event.
const POLL_MS = 10_000;

export function useRuns(status?: string, limit = 50) {
  return useQuery({
    queryKey: runKeys.list(status),
    queryFn: () => api.runs.list({ status, limit }),
    staleTime: 5_000,
    refetchInterval: POLL_MS,
  });
}

export function useRun(id: string | null) {
  return useQuery({
    queryKey: runKeys.detail(id ?? ""),
    queryFn: () => api.runs.get(id!),
    enabled: !!id,
  });
}

/** Every run a message has ever gone through -- the per-message trace view. */
export function useRunsForMail(mailId: string | null) {
  return useQuery({
    queryKey: runKeys.forMail(mailId ?? ""),
    queryFn: () => api.runs.forMail(mailId!),
    enabled: !!mailId,
  });
}

export function useRetryRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.runs.retry(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}
