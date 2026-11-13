/** TanStack Query hooks for the administrator's people and rule-set overview. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AdminUserCreateRequest, AdminUserUpdateRequest } from "@/types/api";

export const adminKeys = {
  users: ["admin", "users"] as const,
  accounts: ["admin", "accounts"] as const,
  davAccounts: ["admin", "dav-accounts"] as const,
  ruleSets: ["admin", "rule-sets"] as const,
};

export function useAdminUsers(options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: adminKeys.users,
    queryFn: () => api.admin.users(),
    staleTime: 15_000,
    enabled: options.enabled ?? true,
  });
}

export function useAdminAccounts() {
  return useQuery({
    queryKey: adminKeys.accounts,
    queryFn: () => api.admin.accounts(),
    staleTime: 15_000,
  });
}

export function useAdminDavAccounts() {
  return useQuery({
    queryKey: adminKeys.davAccounts,
    queryFn: () => api.admin.davAccounts(),
    staleTime: 15_000,
  });
}

/** Every user's rule document as counts, for the Processing page. */
export function useAdminRuleSets() {
  return useQuery({
    queryKey: adminKeys.ruleSets,
    queryFn: () => api.admin.ruleSets(),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

function useInvalidateAdmin() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["admin"] });
}

export function useCreateAdminUser() {
  const invalidate = useInvalidateAdmin();
  return useMutation({
    mutationFn: (data: AdminUserCreateRequest) => api.admin.createUser(data),
    onSuccess: invalidate,
  });
}

export function useUpdateAdminUser() {
  const invalidate = useInvalidateAdmin();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: AdminUserUpdateRequest }) =>
      api.admin.updateUser(id, data),
    onSuccess: invalidate,
  });
}

/** Reassigning an account moves it between users; `kind` picks mail or DAV. */
export function useSetAccountOwner() {
  const invalidate = useInvalidateAdmin();
  return useMutation({
    mutationFn: ({
      kind,
      accountId,
      userId,
    }: {
      kind: "mail" | "dav";
      accountId: string;
      userId: string;
    }) =>
      kind === "mail"
        ? api.admin.setAccountOwner(accountId, userId)
        : api.admin.setDavAccountOwner(accountId, userId),
    onSuccess: invalidate,
  });
}
