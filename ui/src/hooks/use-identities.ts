/** TanStack Query hooks for sending identities (named addresses on a mail account). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const identityKeys = {
  list: (accountId?: string) => ["identities", accountId ?? "all"] as const,
};

export function useIdentities(accountId?: string) {
  return useQuery({
    queryKey: identityKeys.list(accountId),
    queryFn: () => api.identities.list(accountId),
    staleTime: 60_000,
  });
}

export function useCreateIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      account_id: string;
      address: string;
      display_name?: string;
      is_default?: boolean;
    }) => api.identities.create(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["identities"] }),
  });
}

export function useUpdateIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string;
      data: { display_name?: string; is_default?: boolean };
    }) => api.identities.update(id, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["identities"] }),
  });
}

export function useDeleteIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.identities.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["identities"] }),
  });
}
