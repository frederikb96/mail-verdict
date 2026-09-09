/** TanStack Query hooks for account operations. */

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AccountCreateRequest, AccountResponse, AccountUpdateRequest } from "@/types/api";

export const accountKeys = {
  all: ["accounts"] as const,
  detail: (id: string) => ["accounts", id] as const,
};

/**
 * Applies the stored account display order (Settings > Mail > Account
 * order): ordered accounts first, in that order, then any account the
 * order does not yet mention -- never hidden, just appended.
 */
function applyAccountOrder(
  accounts: AccountResponse[],
  order: string[],
): AccountResponse[] {
  const byId = new Map(accounts.map((account) => [account.id, account]));
  const ordered: AccountResponse[] = [];
  for (const id of order) {
    const account = byId.get(id);
    if (account) ordered.push(account);
  }
  const seen = new Set(ordered.map((account) => account.id));
  return [...ordered, ...accounts.filter((account) => !seen.has(account.id))];
}

/** Lists accounts in the order the user has set, wherever they appear. */
export function useAccounts() {
  const accountsQuery = useQuery({
    queryKey: accountKeys.all,
    queryFn: () => api.accounts.list(),
    staleTime: 30_000,
  });
  const orderQuery = useQuery({
    queryKey: ["account-order"],
    queryFn: () => api.accountOrder.get(),
    staleTime: 30_000,
  });
  const order = orderQuery.data?.order;

  // Memoized on the two query results, not recomputed on every call: an
  // effect elsewhere (account-order.tsx) depends on this array, and a
  // fresh reference on every render -- even with identical contents --
  // reruns that effect every render, which sets state and forces another
  // render, forever. That loop starves any Next.js navigation started
  // while it's live, since App Router transitions run at lower priority
  // than the ordinary updates the loop keeps producing.
  const data = useMemo(
    () => (accountsQuery.data ? applyAccountOrder(accountsQuery.data, order ?? []) : accountsQuery.data),
    [accountsQuery.data, order],
  );

  return { ...accountsQuery, data };
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

