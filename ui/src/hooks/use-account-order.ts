/** TanStack Query hooks for the stored account display order. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const accountOrderKeys = {
  get: ["account-order"] as const,
};

export function useAccountOrder() {
  return useQuery({
    queryKey: accountOrderKeys.get,
    queryFn: () => api.accountOrder.get(),
    staleTime: 30_000,
  });
}

/** Mutation to save the account display order. */
export function useUpdateAccountOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (order: string[]) => api.accountOrder.update(order),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: accountOrderKeys.get });
      queryClient.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
}
