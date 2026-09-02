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
import { useAtom } from "jotai";
import { api } from "@/lib/api";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import { useToast } from "@/hooks/use-toast";
import { selectedMailIdAtom } from "@/lib/atoms";
import type {
  FolderOrderResponse,
  FolderResponse,
  MessageActionRequest,
  MessageListResponse,
  MessageSummary,
  ThreadResponse,
} from "@/types/api";

/** Actions that move a message out of the folder it was just shown in. */
const LEAVES_FOLDER_ACTIONS = ["trash", "expunge", "archive", "spam", "not_spam"];

/** Human phrasing for a message/bulk action, used in error toasts. */
export const ACTION_LABELS: Record<string, string> = {
  mark_read: "mark as read",
  mark_unread: "mark as unread",
  flag: "star",
  unflag: "unstar",
  move: "move",
  archive: "archive",
  trash: "move to trash",
  expunge: "delete forever",
  spam: "mark as spam",
  not_spam: "mark as not spam",
  keyword_add: "add keyword",
  keyword_remove: "remove keyword",
};

export const mailKeys = {
  list: (accountId?: string, folderId?: string, threaded?: boolean) =>
    ["mails", accountId, folderId, threaded ? "threaded" : "flat"].filter(
      Boolean,
    ) as string[],
  detail: (id: string) => ["mail", id] as const,
  thread: (id: string) => ["thread", id] as const,
};

export function useMailList(
  accountId: string | null,
  folderId: string | null,
  threaded: boolean,
) {
  return useInfiniteQuery({
    queryKey: mailKeys.list(accountId ?? undefined, folderId ?? undefined, threaded),
    queryFn: ({ pageParam }) =>
      api.mails.list({
        account_id: accountId!,
        folder_id: folderId ?? undefined,
        threaded,
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

export function useMailDetail(mailId: string | null) {
  return useQuery({
    queryKey: mailKeys.detail(mailId!),
    queryFn: () => api.mails.get(mailId!),
    enabled: !!mailId,
    staleTime: 5 * 60_000,
  });
}

/** All messages in a mail's conversation across folders, ascending by date. */
export function useThread(mailId: string | null) {
  return useQuery<ThreadResponse>({
    queryKey: mailKeys.thread(mailId!),
    queryFn: () => api.mails.thread(mailId!),
    enabled: !!mailId,
    staleTime: 30_000,
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
  // Selected mail lives in the same store every action initiator (list row,
  // reading pane, bulk toolbar) reads from, so clearing it here reaches all
  // of them: once the open message leaves its folder, nothing keeps acting
  // on it under a reading pane that still shows its old content.
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const { push: pushToast } = useToast();

  return useMutation({
    mutationFn: ({
      mailId,
      action,
    }: {
      mailId: string;
      accountId: string;
      action: MessageActionRequest;
    }) => api.mails.action(mailId, action),

    onMutate: async ({ mailId, accountId, action }) => {
      await qc.cancelQueries({ queryKey: ["mails"] });
      await qc.cancelQueries({ queryKey: ["folders"] });

      const act = action.action;
      const removesFromList = LEAVES_FOLDER_ACTIONS.includes(act);
      const wasSelected = removesFromList && mailId === selectedMailId;
      if (wasSelected) setSelectedMailId(null);

      const mailInfo = findMailInCache(qc, mailId);
      if (!mailInfo) return { wasSelected, mailId };

      const prevMailQueries = qc.getQueriesData({ queryKey: ["mails"] });
      const prevFolders = qc.getQueryData(["folders", accountId]);
      const prevMailDetail = qc.getQueryData(["mail", mailId]);

      if (removesFromList) {
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
      } else if (act === "move") {
        updateMailInCache(qc, mailId, { pending_sync: true });
      }

      // Update detail cache
      if (prevMailDetail && !removesFromList) {
        const updates: Partial<MessageSummary> = {};
        if (act === "flag") updates.is_flagged = true;
        if (act === "unflag") updates.is_flagged = false;
        if (act === "mark_read") updates.is_seen = true;
        if (act === "mark_unread") updates.is_seen = false;
        if (act === "move") updates.pending_sync = true;
        qc.setQueryData(["mail", mailId], {
          ...(prevMailDetail as Record<string, unknown>),
          ...updates,
        });
      }

      return { prevMailQueries, prevFolders, prevMailDetail, accountId, mailId, wasSelected };
    },

    onError: (err, vars, ctx) => {
      const label = ACTION_LABELS[vars.action.action] ?? vars.action.action;
      pushToast(`Could not ${label}: ${err.message}`, "error", 0);

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
      if (ctx.wasSelected && ctx.mailId) {
        setSelectedMailId(ctx.mailId);
      }
    },

    onSettled: (_data, _err, { mailId }) => {
      qc.invalidateQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["mail"] });
      qc.invalidateQueries({ queryKey: ["thread", mailId] });
      invalidateAllFolderCaches(qc);
    },
  });
}
