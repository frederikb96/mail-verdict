/** TanStack Query hooks for job operations. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const jobKeys = {
  all: ["jobs"] as const,
};

export function useJobs() {
  return useQuery({
    queryKey: jobKeys.all,
    queryFn: () => api.jobs.list(),
    staleTime: 10_000,
  });
}

export function useStartJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      name,
      accountId,
    }: {
      name: string;
      accountId?: string;
    }) => api.jobs.start(name, accountId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: jobKeys.all });
    },
  });
}

export function useStopJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      name,
      accountId,
    }: {
      name: string;
      accountId?: string;
    }) => api.jobs.stop(name, accountId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: jobKeys.all });
    },
  });
}
