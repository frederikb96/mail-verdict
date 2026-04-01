/** TanStack Query hooks for mail operations. */

import {
  type InfiniteData,
  type QueryClient,
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import type {
  FolderOrderResponse,
  FolderResponse,
  MessageActionRequest,
  MessageListResponse,
  MessageSummary,
} from "@/types/api";

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
    enabled: !!accountId && !!folderId,
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

/** Find a mail's metadata from the infinite query cache. */
function findMailInCache(qc: QueryClient, mailId: string) {
  const queries = qc.getQueriesData<InfiniteData<MessageListResponse>>({
    queryKey: ["mails"],
  });
  for (const [, data] of queries) {
    if (!data?.pages) continue;
    for (const page of data.pages) {
      const mail = page.messages.find((m) => m.id === mailId);
      if (mail)
        return {
          folderId: mail.folder_id,
          isSeen: mail.is_seen,
          isFlagged: mail.is_flagged,
        };
    }
  }
  return null;
}

/** Remove a mail from all infinite query caches. */
function removeMailFromCache(qc: QueryClient, mailId: string) {
  qc.setQueriesData<InfiniteData<MessageListResponse>>(
    { queryKey: ["mails"] },
    (old) => {
      if (!old) return old;
      return {
        ...old,
        pages: old.pages.map((page) => ({
          ...page,
          messages: page.messages.filter((m) => m.id !== mailId),
        })),
      };
    },
  );
}

/** Update a mail's properties in all infinite query caches. */
function updateMailInCache(
  qc: QueryClient,
  mailId: string,
  updates: Partial<MessageSummary>,
) {
  qc.setQueriesData<InfiniteData<MessageListResponse>>(
    { queryKey: ["mails"] },
    (old) => {
      if (!old) return old;
      return {
        ...old,
        pages: old.pages.map((page) => ({
          ...page,
          messages: page.messages.map((m) =>
            m.id === mailId ? { ...m, ...updates } : m,
          ),
        })),
      };
    },
  );
}

/** Adjust folder total_count and unread_count in ALL folder caches. */
export function updateFolderCounts(
  qc: QueryClient,
  accountId: string,
  folderId: string,
  totalDelta: number,
  unreadDelta: number,
) {
  const applyDelta = (total: number, unread: number) => ({
    total_count: Math.max(0, total + totalDelta),
    unread_count: Math.max(0, unread + unreadDelta),
  });

  qc.setQueryData<FolderResponse[]>(["folders", accountId], (old) => {
    if (!old) return old;
    return old.map((f) =>
      f.id === folderId ? { ...f, ...applyDelta(f.total_count, f.unread_count) } : f,
    );
  });

  qc.setQueryData<FolderOrderResponse>(["folder-order", accountId], (old) => {
    if (!old) return old;
    return {
      ...old,
      folders: old.folders.map((f) =>
        f.folder_id === folderId
          ? { ...f, ...applyDelta(f.total_count, f.unread_count) }
          : f,
      ),
    };
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
      action: MessageActionRequest;
    }) => api.mails.action(mailId, accountId, action),

    onMutate: async ({ mailId, accountId, action }) => {
      await qc.cancelQueries({ queryKey: ["mails"] });
      await qc.cancelQueries({ queryKey: ["folders"] });

      const mailInfo = findMailInCache(qc, mailId);
      if (!mailInfo) return {};

      const prevMailQueries = qc.getQueriesData({ queryKey: ["mails"] });
      const prevFolders = qc.getQueryData(["folders", accountId]);
      const prevMailDetail = qc.getQueryData(["mail", mailId]);

      const act = action.action;

      if (["delete", "archive", "spam"].includes(act)) {
        removeMailFromCache(qc, mailId);
        updateFolderCounts(
          qc,
          accountId,
          mailInfo.folderId,
          -1,
          mailInfo.isSeen ? 0 : -1,
        );
      } else if (act === "flag") {
        updateMailInCache(qc, mailId, { is_flagged: true });
      } else if (act === "unflag") {
        updateMailInCache(qc, mailId, { is_flagged: false });
      } else if (act === "mark_read") {
        updateMailInCache(qc, mailId, { is_seen: true });
        if (!mailInfo.isSeen)
          updateFolderCounts(qc, accountId, mailInfo.folderId, 0, -1);
      } else if (act === "mark_unread") {
        updateMailInCache(qc, mailId, { is_seen: false });
        if (mailInfo.isSeen)
          updateFolderCounts(qc, accountId, mailInfo.folderId, 0, 1);
      }

      // Update detail cache
      if (prevMailDetail && !["delete", "archive", "spam"].includes(act)) {
        const updates: Partial<MessageSummary> = {};
        if (act === "flag") updates.is_flagged = true;
        if (act === "unflag") updates.is_flagged = false;
        if (act === "mark_read") updates.is_seen = true;
        if (act === "mark_unread") updates.is_seen = false;
        qc.setQueryData(["mail", mailId], {
          ...(prevMailDetail as Record<string, unknown>),
          ...updates,
        });
      }

      return { prevMailQueries, prevFolders, prevMailDetail, accountId, mailId };
    },

    onError: (_err, _vars, ctx) => {
      if (!ctx) return;
      if (ctx.prevMailQueries) {
        for (const [key, data] of ctx.prevMailQueries as Array<
          [readonly unknown[], unknown]
        >) {
          qc.setQueryData(key, data);
        }
      }
      if (ctx.prevFolders && ctx.accountId) {
        qc.setQueryData(["folders", ctx.accountId], ctx.prevFolders);
      }
      if (ctx.prevMailDetail && ctx.mailId) {
        qc.setQueryData(["mail", ctx.mailId], ctx.prevMailDetail);
      }
    },

    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["mail"] });
      invalidateAllFolderCaches(qc);
    },
  });
}
