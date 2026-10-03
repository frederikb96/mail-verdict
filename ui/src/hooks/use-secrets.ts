/** TanStack Query hooks for the secret store. Values only ever travel
 * outward in a write; nothing here can read one back. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const secretKeys = {
  list: ["secrets"] as const,
};

export function useSecrets() {
  return useQuery({
    queryKey: secretKeys.list,
    queryFn: () => api.secrets.list(),
    staleTime: 60_000,
  });
}

export function usePutSecret() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, value }: { name: string; value: string }) =>
      api.secrets.put(name, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: secretKeys.list }),
  });
}

export function useDeleteSecret() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.secrets.delete(name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: secretKeys.list }),
  });
}
