/** TanStack Query hooks for account operations. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AccountCreateRequest, AccountUpdateRequest } from "@/types/api";

export const accountKeys = {
  all: ["accounts"] as const,
  detail: (id: string) => ["accounts", id] as const,
};

export function useAccounts() {
  return useQuery({
    queryKey: accountKeys.all,
    queryFn: () => api.accounts.list(),
    staleTime: 30_000,
  });
}

export function useAccount(id: string | null) {
  return useQuery({
    queryKey: accountKeys.detail(id!),
    queryFn: () => api.accounts.get(id!),
    enabled: !!id,
    staleTime: 5 * 60_000,
  });
}

export function useCreateAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: AccountCreateRequest) => api.accounts.create(data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: accountKeys.all });
    },
  });
}

export function useUpdateAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: AccountUpdateRequest }) =>
      api.accounts.update(id, data),
    onSuccess: (_data, { id }) => {
      qc.invalidateQueries({ queryKey: accountKeys.all });
      qc.invalidateQueries({ queryKey: accountKeys.detail(id) });
    },
  });
}

export function useDeleteAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.accounts.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: accountKeys.all });
    },
  });
}

export function useTestConnection() {
  return useMutation({
    mutationFn: (id: string) => api.accounts.testConnection(id),
  });
}

export function useTriggerSync() {
  return useMutation({
    mutationFn: (id: string) => api.accounts.triggerSync(id),
  });
}

export function useCancelSync() {
  return useMutation({
    mutationFn: (id: string) => api.accounts.cancelSync(id),
  });
}
