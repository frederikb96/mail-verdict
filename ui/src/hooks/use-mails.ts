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
import { useAtom, useAtomValue } from "jotai";
import { api } from "@/lib/api";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import { useToast } from "@/hooks/use-toast";
import { activeReplyDirtyForThreadIdAtom, selectedMailIdAtom } from "@/lib/atoms";
import type {
  FolderOrderResponse,
  FolderResponse,
  MessageActionRequest,
  MessageListResponse,
  MessageQuoteResponse,
  MessageSummary,
  ThreadResponse,
} from "@/types/api";

/** Actions that move a message out of the folder it was just shown in. */
const LEAVES_FOLDER_ACTIONS = ["trash", "expunge", "archive", "spam", "not_spam"];

/**
 * Destructive actions offered with an "Undo" toast on success -- moving the
 * message straight back to the folder it was in is the compensating action,
 * the same shape a failed mutation's own rollback already uses. `expunge`
 * has no compensating action (there is nothing left to move back) and
 * `not_spam` already is the corrective action for a wrong `spam` verdict.
 */
export const UNDOABLE_ACTIONS = ["trash", "archive", "spam"];

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

/** Human phrasing for the success toast a completed undoable action shows. */
export const UNDO_TOAST_LABELS: Record<string, string> = {
  trash: "Moved to trash",
  archive: "Archived",
  spam: "Marked as spam",
};

export const mailKeys = {
  list: (accountId?: string, folderId?: string, threaded?: boolean, aroundId?: string) =>
    ["mails", accountId, folderId, threaded ? "threaded" : "flat", aroundId].filter(
      Boolean,
    ) as string[],
  detail: (id: string) => ["mail", id] as const,
  thread: (id: string) => ["thread", id] as const,
  quote: (id: string) => ["mail-quote", id] as const,
};

/**
 * A live-mail-list infinite query is refetched by re-fetching every
 * already-loaded page in sequence (TanStack has no partial-refetch for an
 * infinite query) -- so invalidating, refocusing, or remounting one
 * scrolled hundreds of pages deep would turn a single arriving message,
 * or simply switching back to this tab, into hundreds of requests.
 * `hasFewEnoughPagesToEagerlyRefetch` below is the one predicate every
 * trigger is gated on, so a list past this depth never refetches eagerly
 * regardless of which of the three actually fires; a shallowly-loaded list
 * (the overwhelmingly common case) still refreshes immediately by any of
 * them.
 *
 * Past the bound, a list stays stale until its query is genuinely reset,
 * not merely re-observed -- switching to another folder and back does
 * NOT do this: the query keeps its cached pages for a full day
 * (`gcTime`, in providers.tsx) with no observer, and `refetchOnMount`
 * below is gated by this same bound, so remounting a still-deep query
 * skips the refetch exactly as a background tab regaining focus would.
 * What actually clears it: a full page reload (this query is excluded
 * from the persisted cache, so a reload observes it with nothing in it
 * and an uninitialized query always fetches, this bound or no), or any
 * bulk action anywhere finishing, since its own completion resets every
 * mail-list query -- including one the reader isn't currently looking at
 * -- back to page one.
 */
const EAGER_REFETCH_MAX_PAGES = 3;

/** Shared by the SSE-driven invalidate below and by useMailList's/
 * useUnifiedMails's own refetchOnWindowFocus/refetchOnMount -- see
 * EAGER_REFETCH_MAX_PAGES's docstring for why the same bound has to gate
 * all three rather than only the one it was first written for. */
export function hasFewEnoughPagesToEagerlyRefetch(query: { state: { data?: unknown } }): boolean {
  const data = query.state.data as { pages?: unknown[] } | undefined;
  return (data?.pages?.length ?? 0) <= EAGER_REFETCH_MAX_PAGES;
}

/**
 * One page's fetch shape -- opaque to the caller, threaded through
 * TanStack's own pageParam rather than read from anywhere else, so a
 * refetch (SSE-triggered or otherwise) always re-issues the exact
 * request that produced the page it's replacing. "initial"/"around" only
 * ever appears as the very first page's own param; every later page is
 * "before" (continuing older) or "after" (continuing newer, only
 * reachable once a page ever opened away from the newest edge).
 */
type MailListPageParam =
  | { kind: "initial" }
  | { kind: "around"; id: string }
  | { kind: "before"; cursor: string }
  | { kind: "after"; cursor: string };

/**
 * aroundId centres the *first* fetch of a fresh query key on that message
 * instead of the newest edge -- see mail-list.tsx, which captures it once
 * per list identity rather than re-reading it reactively, and folds it
 * into the query key so a centred window is a genuinely different cached
 * list from an edge-anchored one under the same account/folder.
 */
export function useMailList(
  accountId: string | null,
  folderId: string | null,
  threaded: boolean,
  aroundId?: string | null,
) {
  return useInfiniteQuery({
    queryKey: mailKeys.list(
      accountId ?? undefined, folderId ?? undefined, threaded, aroundId ?? undefined,
    ),
    queryFn: ({ pageParam }: { pageParam: MailListPageParam }) => {
      const base = { account_id: accountId!, folder_id: folderId ?? undefined, threaded };
      switch (pageParam.kind) {
        case "around":
          return api.mails.list({ ...base, around: pageParam.id, limit: 50 });
        case "before":
          return api.mails.list({ ...base, before: pageParam.cursor, limit: 50 });
        case "after":
          return api.mails.list({ ...base, after: pageParam.cursor, limit: 50 });
        case "initial":
          return api.mails.list({ ...base, limit: 50 });
      }
    },
    initialPageParam: (
      aroundId ? { kind: "around", id: aroundId } : { kind: "initial" }
    ) as MailListPageParam,
    getNextPageParam: (lastPage): MailListPageParam | undefined =>
      lastPage.has_more ? { kind: "before", cursor: lastPage.next_cursor! } : undefined,
    getPreviousPageParam: (firstPage): MailListPageParam | undefined =>
      firstPage.has_more_newer ? { kind: "after", cursor: firstPage.prev_cursor! } : undefined,
    enabled: !!accountId && !!folderId,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
    refetchOnWindowFocus: hasFewEnoughPagesToEagerlyRefetch,
    refetchOnMount: hasFewEnoughPagesToEagerlyRefetch,
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

/** A message's body as safe-to-send HTML, for reopening a draft or
 * embedding it as a reply/forward quote -- see draft-editor.tsx and
 * reply-box.tsx. staleTime: Infinity, since the underlying message never
 * changes once it exists and this is fetched fresh on every mount anyway
 * (a compose surface never stays open long enough for the cache's
 * default staleness to matter). */
export function useMessageQuote(mailId: string | null) {
  return useQuery<MessageQuoteResponse>({
    queryKey: mailKeys.quote(mailId!),
    queryFn: () => api.mails.quote(mailId!),
    enabled: !!mailId,
    staleTime: Infinity,
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
          threadId: mail.thread_id,
        };
    }
  }
  return null;
}

/** Remove a mail from all infinite query caches. */
export function removeMailFromCache(qc: QueryClient, mailId: string) {
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

/**
 * Remove a mail from every list cache, including the unified view's --
 * unlike removeMailFromCache above (single-account mutations only ever
 * need to patch their own account's lists), an SSE mail.deleted can
 * concern a message the unified view is currently showing.
 */
export function removeMailFromAllListCaches(qc: QueryClient, mailId: string) {
  removeMailFromCache(qc, mailId);
  qc.setQueriesData<InfiniteData<{ messages: { id: string }[] }>>(
    { queryKey: ["unified", "mails"] },
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

/**
 * Update a mail's properties in every infinite query cache that derives
 * from it -- the per-account/folder list and the unified view's, which
 * carries its own copy of the same fields under a different query key.
 * Missing the unified branch here is the same bug as missing the thread
 * cache below: three caches hold the same fact, and a patch that only
 * reaches two of them leaves whichever screen reads the third showing
 * stale data until the next unrelated refetch settles it.
 */
export function updateMailInCache(
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
  qc.setQueriesData<InfiniteData<{ messages: Array<{ id: string }> }>>(
    { queryKey: ["unified", "mails"] },
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

/**
 * Update a mail's properties in every ["thread", *] cache that currently
 * holds it -- a thread is cached under the id it was first opened with,
 * so the same message can appear in more than one such cache (or in
 * none, if its thread was never opened). The reading pane's header
 * controls read from this cache; the list row reads from the caches
 * updateMailInCache above patches. Both describe the same fact and must
 * change together, or the two disagree for a full round trip after any
 * action -- this is what read as the mark-unread button "flipping back".
 */
export function updateMailInThreadCaches(
  qc: QueryClient,
  mailId: string,
  updates: Partial<MessageSummary>,
) {
  qc.setQueriesData<ThreadResponse>({ queryKey: ["thread"] }, (old) => {
    if (!old || !old.messages.some((m) => m.id === mailId)) return old;
    return {
      ...old,
      messages: old.messages.map((m) =>
        m.id === mailId ? { ...m, ...updates } : m,
      ),
    };
  });
}

export function invalidateMailListsBounded(qc: QueryClient): void {
  for (const prefix of [["mails"], ["unified", "mails"]] as const) {
    for (const query of qc.getQueryCache().findAll({ queryKey: prefix })) {
      qc.invalidateQueries({
        queryKey: query.queryKey,
        exact: true,
        refetchType: hasFewEnoughPagesToEagerlyRefetch(query) ? "active" : "none",
      });
    }
  }
}

/**
 * A mail.updated SSE event names which columns changed but not their new
 * values (the underlying NOTIFY payload carries only column names) -- so
 * reflecting it needs one fetch of that message, never a full list
 * refetch. Patches every list cache holding the row plus its own detail
 * cache from the same response. Swallows a 404: the message may already
 * be gone by the time this runs, and the list settles on its own bounded
 * refetch regardless.
 */
export async function refreshMailFromServer(qc: QueryClient, mailId: string): Promise<void> {
  try {
    const detail = await qc.fetchQuery({
      queryKey: mailKeys.detail(mailId),
      queryFn: () => api.mails.get(mailId),
      staleTime: 0,
    });
    updateMailInCache(qc, mailId, {
      is_seen: detail.is_seen,
      is_flagged: detail.is_flagged,
      is_answered: detail.is_answered,
    });
  } catch {
    // Ignore -- see docstring.
  }
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
  // on it under a reading pane that still shows its old content -- except
  // a reply or forward in progress against its thread, which the clear
  // would take down too. See activeReplyDirtyForThreadId below.
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const activeReplyDirtyForThreadId = useAtomValue(activeReplyDirtyForThreadIdAtom);
  const { push: pushToast } = useToast();

  const mailAction = useMutation({
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
      const mailInfo = findMailInCache(qc, mailId);
      // A reply or forward in progress against this message's thread must
      // not be discarded by unmounting the reading pane out from under it
      // -- reply-box.tsx is what sets this atom while dirty. Matched on
      // the thread rather than requiring mailId itself to be the reply's
      // source: the reply always targets the thread's newest message,
      // while the reading pane's own "open" message (mailId here) can be
      // an older one the reader expanded, and trashing that older one
      // must not throw the reply away either. The action itself still
      // goes through (trashing from a row is independent of whatever is
      // being typed below it); only the selection stays put.
      const hasDirtyReply =
        mailInfo != null && mailInfo.threadId === activeReplyDirtyForThreadId;
      const wasSelected = removesFromList && mailId === selectedMailId && !hasDirtyReply;
      if (wasSelected) setSelectedMailId(null);

      if (!mailInfo) return { wasSelected, mailId };

      const prevMailQueries = qc.getQueriesData({ queryKey: ["mails"] });
      const prevThreadQueries = qc.getQueriesData({ queryKey: ["thread"] });
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
      } else {
        // Computed once and applied to every cache that derives from the
        // same fact -- the list row, the reading pane's thread cache, and
        // the single-message detail cache -- rather than three places each
        // deciding "what changed" and drifting apart. Folder unread counts
        // are the one derived value that isn't a plain field copy, so they
        // stay their own branch below.
        const updates: Partial<MessageSummary> = {};
        if (act === "flag") updates.is_flagged = true;
        if (act === "unflag") updates.is_flagged = false;
        if (act === "mark_read") updates.is_seen = true;
        if (act === "mark_unread") updates.is_seen = false;
        if (act === "move") updates.pending_sync = true;

        updateMailInCache(qc, mailId, updates);
        updateMailInThreadCaches(qc, mailId, updates);
        if (prevMailDetail) {
          qc.setQueryData(["mail", mailId], {
            ...(prevMailDetail as Record<string, unknown>),
            ...updates,
          });
        }

        if (act === "mark_read" && !mailInfo.isSeen)
          updateFolderCounts(qc, accountId, mailInfo.folderId, 0, -1);
        if (act === "mark_unread" && mailInfo.isSeen)
          updateFolderCounts(qc, accountId, mailInfo.folderId, 0, 1);
      }

      return {
        prevMailQueries, prevThreadQueries, prevFolders, prevMailDetail, accountId, mailId,
        wasSelected, originalFolderId: mailInfo.folderId,
      };
    },

    onSuccess: (_data, { action }, ctx) => {
      if (!ctx?.originalFolderId || !UNDOABLE_ACTIONS.includes(action.action)) return;
      const { accountId, mailId, originalFolderId } = ctx;
      pushToast(UNDO_TOAST_LABELS[action.action], "success", 6000, {
        label: "Undo",
        onClick: () =>
          mailAction.mutate({
            mailId,
            accountId,
            action: { action: "move", target_folder_id: originalFolderId },
          }),
      });
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
      if (ctx.prevThreadQueries) {
        for (const [key, data] of ctx.prevThreadQueries as Array<
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

  return mailAction;
}
