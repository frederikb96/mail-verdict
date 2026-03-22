/** TanStack Query hooks for mail operations. */

import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { MailActionRequest } from "@/types/api";

export const mailKeys = {
  list: (accountId?: string, folderId?: string) =>
    ["mails", accountId, folderId].filter(Boolean) as string[],
  detail: (id: string) => ["mail", id] as const,
};

export function useMailList(accountId: string | null, folderId: string | null) {
  return useInfiniteQuery({
    queryKey: mailKeys.list(accountId ?? undefined, folderId ?? undefined),
    queryFn: ({ pageParam }) =>
      api.mails.list({
        account_id: accountId ?? undefined,
        folder_id: folderId ?? undefined,
        before: pageParam ?? undefined,
        limit: 50,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? lastPage.next_cursor : undefined,
    enabled: !!accountId,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });
}

export function useMailDetail(mailId: string | null, accountId: string | null) {
  return useQuery({
    queryKey: mailKeys.detail(mailId!),
    queryFn: () => api.mails.get(mailId!, accountId!),
    enabled: !!mailId && !!accountId,
    staleTime: 5 * 60_000,
  });
}

export function useMailAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      mailId,
      accountId,
      action,
    }: {
      mailId: string;
      accountId: string;
      action: MailActionRequest;
    }) => api.mails.action(mailId, accountId, action),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["mail"] });
      qc.invalidateQueries({ queryKey: ["folders"] });
    },
  });
}
