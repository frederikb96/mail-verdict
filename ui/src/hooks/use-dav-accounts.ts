/** TanStack Query hooks for CalDAV/CardDAV accounts. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { DavAccountCreateRequest, DavAccountUpdateRequest } from "@/types/api";

export const davAccountKeys = {
  list: ["dav-accounts"] as const,
};

export function useDavAccounts() {
  return useQuery({
    queryKey: davAccountKeys.list,
    queryFn: () => api.davAccounts.list(),
    staleTime: 30_000,
  });
}

export function useCreateDavAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: DavAccountCreateRequest) => api.davAccounts.create(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: davAccountKeys.list }),
  });
}

export function useUpdateDavAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: DavAccountUpdateRequest }) =>
      api.davAccounts.update(id, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: davAccountKeys.list }),
  });
}

export function useDeleteDavAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.davAccounts.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: davAccountKeys.list }),
  });
}

export function useTriggerDavSync() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.davAccounts.triggerSync(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: davAccountKeys.list }),
  });
}
