/** TanStack Query hooks for mail operations. */

import { useCallback, useEffect } from "react";
import {
  type InfiniteData,
  type Query,
  type QueryClient,
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { api } from "@/lib/api";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import { useToast } from "@/hooks/use-toast";
import {
  type WindowRow,
  chunkIntoPages,
  mergeRefreshedWindow,
  windowRefreshLimit,
} from "@/lib/mail-list-window";
import { isMailListQuery } from "@/lib/query-persister";
import {
  activeReplyDirtyForThreadIdAtom,
  explicitlyUnreadMailIdAtom,
  selectedMailIdAtom,
} from "@/lib/atoms";
import { type MailNavDirection, mailNavDirectionAtom } from "@/store/mail-nav-atom";
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
 * A list opened around a message rather than at the newest edge (see
 * useMailList's `aroundId`) is refetched the ordinary TanStack way, which
 * re-reads every loaded page one request at a time -- so only while it is
 * this shallow. A list at the newest edge never is: refreshMailViews below
 * re-reads its whole loaded window in one request, however deep.
 */
const AROUND_WINDOW_EAGER_REFETCH_MAX_PAGES = 3;

function aroundWindowIsShallow(query: { state: { data?: unknown } }): boolean {
  const data = query.state.data as { pages?: unknown[] } | undefined;
  return (data?.pages?.length ?? 0) <= AROUND_WINDOW_EAGER_REFETCH_MAX_PAGES;
}

/**
 * Which list a mail-list query at the newest edge holds -- carried in the
 * query's own `meta`, so a refresh can re-read it without parsing the key.
 */
export type MailListWindow =
  | { kind: "account"; accountId: string; folderId: string; threaded: boolean }
  | { kind: "unified"; folderName: string };

type WindowPage = { messages: WindowRow[]; has_more: boolean };

function windowOf(query: Query): MailListWindow | undefined {
  return query.meta?.mailListWindow as MailListWindow | undefined;
}

async function readWindow(
  source: MailListWindow,
  limit: number,
): Promise<{ rows: WindowRow[]; hasMore: boolean }> {
  const page =
    source.kind === "account"
      ? await api.mails.list({
          account_id: source.accountId,
          folder_id: source.folderId,
          threaded: source.threaded,
          limit,
        })
      : await api.unified.mails({ folder_name: source.folderName, limit });
  return { rows: page.messages, hasMore: page.has_more };
}

/** Rows back into the page shape each list's own infinite query holds, with
 * the page params its own paging would have produced -- so the next page it
 * fetches continues from the right row. */
function windowAsInfiniteData(
  source: MailListWindow,
  rows: WindowRow[],
  hasMore: boolean,
): InfiniteData<unknown, unknown> {
  const pages = chunkIntoPages(rows);
  const last = pages.length - 1;
  const lastIdOf = (page: WindowRow[]) => page[page.length - 1]?.id ?? null;
  const pageHasMore = (i: number) => (i < last || hasMore) && lastIdOf(pages[i]) !== null;
  const nextCursor = (i: number) => (pageHasMore(i) ? lastIdOf(pages[i]) : null);
  if (source.kind === "account") {
    return {
      pages: pages.map((messages, i) => ({
        messages,
        has_more: pageHasMore(i),
        next_cursor: nextCursor(i),
        has_more_newer: false,
        prev_cursor: null,
      })),
      pageParams: pages.map((_, i) =>
        i === 0 ? { kind: "initial" } : { kind: "before", cursor: lastIdOf(pages[i - 1]) },
      ),
    };
  }
  return {
    pages: pages.map((messages, i) => ({
      messages,
      has_more: pageHasMore(i),
      next_cursor: nextCursor(i),
    })),
    pageParams: pages.map((_, i) => (i === 0 ? undefined : lastIdOf(pages[i - 1]))),
  };
}

/** Resolves once the query has no fetch of its own in flight. */
function whenQueryIdle(qc: QueryClient, query: Query): Promise<void> {
  if (query.state.fetchStatus !== "fetching") return Promise.resolve();
  return new Promise((resolve) => {
    const unsubscribe = qc.getQueryCache().subscribe((event) => {
      if (event.query !== query) return;
      if (event.type === "removed" || query.state.fetchStatus !== "fetching") {
        unsubscribe();
        resolve();
      }
    });
  });
}

async function refreshWindowOnce(
  qc: QueryClient,
  query: Query,
  source: MailListWindow,
): Promise<void> {
  const loaded = query.state.data as InfiniteData<WindowPage> | undefined;
  if (!loaded) return;
  const loadedRows = loaded.pages.reduce((n, page) => n + page.messages.length, 0);
  let fresh: { rows: WindowRow[]; hasMore: boolean };
  try {
    fresh = await readWindow(source, windowRefreshLimit(loadedRows));
  } catch {
    // The next change, focus or reconnect refreshes it again.
    return;
  }
  // Merged into what the cache holds now rather than what it held when the
  // read began, so a page the reader fetched meanwhile is kept.
  const current = qc.getQueryData<InfiniteData<WindowPage>>(query.queryKey);
  if (!current) return;
  const merged = mergeRefreshedWindow(
    current.pages.flatMap((page) => page.messages),
    fresh.rows,
    fresh.hasMore,
    current.pages[current.pages.length - 1]?.has_more ?? false,
  );
  qc.setQueryData(query.queryKey, windowAsInfiniteData(source, merged.rows, merged.hasMore));
}

const windowRefreshes = new WeakMap<Query, { again: boolean }>();

/**
 * Re-read one list's whole loaded window. Never more than one read per list
 * in flight: a change arriving meanwhile queues exactly one more. A page
 * fetch of the list's own is waited out first, and the window read again if
 * one started during the read -- TanStack writes a fetched page on top of
 * the pages it began from, which would put back whatever this replaced.
 */
function refreshWindow(qc: QueryClient, query: Query): void {
  const source = windowOf(query);
  if (!source) return;
  const running = windowRefreshes.get(query);
  if (running) {
    running.again = true;
    return;
  }
  const state = { again: true };
  windowRefreshes.set(query, state);
  void (async () => {
    try {
      while (state.again) {
        state.again = false;
        await whenQueryIdle(qc, query);
        await refreshWindowOnce(qc, query, source);
        if (query.state.fetchStatus === "fetching") state.again = true;
      }
    } finally {
      windowRefreshes.delete(query);
    }
  })();
}

/**
 * Bring every open mail list up to date -- the list half of
 * refreshMailViews on its own, for a window regaining focus, where the
 * counts already refetch through their own queries. A list nobody is
 * looking at is refreshed when it is shown again (useRefreshWindowOnMount).
 */
export function refreshMailLists(qc: QueryClient): void {
  const lists = qc.getQueryCache().findAll({ predicate: (q) => isMailListQuery(q.queryKey) });
  for (const query of lists) {
    if (windowOf(query)) {
      if (query.getObserversCount() > 0) refreshWindow(qc, query);
      continue;
    }
    qc.invalidateQueries({
      queryKey: query.queryKey,
      exact: true,
      refetchType: aroundWindowIsShallow(query) ? "active" : "none",
    });
  }
}

/**
 * The one way the mail lists and every count beside them are brought up to
 * date after mail changed -- a live event, an action settling, a reconnect.
 * Both halves are refreshed by the same call at the same moment, so no path
 * can refresh the counts and leave a list behind.
 */
export function refreshMailViews(qc: QueryClient): void {
  refreshMailLists(qc);
  invalidateAllFolderCaches(qc);
}

/**
 * A list shown again holds whatever its cache kept while it was away; one
 * refresh of its window on mount brings it up to date. TanStack's own
 * refetch-on-mount would re-read every loaded page, one request each.
 */
export function useRefreshWindowOnMount(queryKey: readonly unknown[], enabled: boolean): void {
  const qc = useQueryClient();
  const keyHash = JSON.stringify(queryKey);
  useEffect(() => {
    if (!enabled) return;
    const query = qc.getQueryCache().find({ queryKey, exact: true });
    if (query?.state.data !== undefined) refreshWindow(qc, query);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qc, keyHash, enabled]);
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
  const queryKey = mailKeys.list(
    accountId ?? undefined, folderId ?? undefined, threaded, aroundId ?? undefined,
  );
  // Only a list starting at the newest edge is refreshed as one window; one
  // opened around a message keeps TanStack's own refetch, bounded.
  const listWindow: MailListWindow | undefined =
    accountId && folderId && !aroundId
      ? { kind: "account", accountId, folderId, threaded }
      : undefined;
  const result = useInfiniteQuery({
    queryKey,
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
    meta: listWindow ? { mailListWindow: listWindow } : undefined,
    refetchOnWindowFocus: listWindow ? false : aroundWindowIsShallow,
    refetchOnMount: listWindow ? false : aroundWindowIsShallow,
  });
  useRefreshWindowOnMount(queryKey, listWindow !== undefined);
  return result;
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

/**
 * The message that should take the reader's place when `mailId` leaves the
 * list: its neighbour in `direction`, or the one on the other side when
 * there is nothing that way. Null when it was the only one loaded.
 *
 * Read out of the list caches rather than passed in, so every surface that
 * can remove the open message -- a row's own control, the reading pane's
 * toolbar, a keyboard shortcut -- lands on the same next message without
 * each deciding for itself.
 */
function neighbourInCache(
  qc: QueryClient,
  mailId: string,
  direction: MailNavDirection,
): string | null {
  const queries = [
    ...qc.getQueriesData<InfiniteData<{ messages: { id: string }[] }>>({
      queryKey: ["mails"],
    }),
    ...qc.getQueriesData<InfiniteData<{ messages: { id: string }[] }>>({
      queryKey: ["unified", "mails"],
    }),
  ];
  for (const [, data] of queries) {
    if (!data?.pages) continue;
    const ids = data.pages.flatMap((page) => page.messages.map((m) => m.id));
    const at = ids.indexOf(mailId);
    if (at < 0) continue;
    const step = direction === "older" ? 1 : -1;
    return ids[at + step] ?? ids[at - step] ?? null;
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

/**
 * Adjust a conversation row's own unread count in every list cache holding
 * it as a conversation -- only rows fetched grouped carry the count, so a
 * flat list's row of the same message is left alone.
 */
function updateConversationUnread(
  qc: QueryClient,
  rowId: string,
  next: (unread: number) => number,
) {
  qc.setQueriesData<InfiniteData<MessageListResponse>>({ queryKey: ["mails"] }, (old) => {
    if (!old) return old;
    return {
      ...old,
      pages: old.pages.map((page) => ({
        ...page,
        messages: page.messages.map((m) =>
          m.id === rowId && m.unread_in_thread !== undefined
            ? { ...m, unread_in_thread: Math.max(0, next(m.unread_in_thread)) }
            : m,
        ),
      })),
    };
  });
}

/**
 * Mark read every unread message of a conversation row's thread in that
 * row's folder -- what reading a row grouped by conversation means, since
 * the row counts all of them (isRowUnread). The row's own message is left
 * out unless `includeRow`: opening a row already marks it read through the
 * reading pane.
 */
export function useMarkConversationRead() {
  const qc = useQueryClient();
  const { push: pushToast } = useToast();
  return useCallback(
    async (row: MessageSummary, includeRow: boolean) => {
      let thread: ThreadResponse;
      try {
        thread = await qc.fetchQuery({
          queryKey: mailKeys.thread(row.id),
          queryFn: () => api.mails.thread(row.id),
          staleTime: 0,
        });
      } catch (err) {
        pushToast(`Could not mark as read: ${(err as Error).message}`, "error", 0);
        return;
      }
      const ids = thread.messages
        .filter((m) => m.folder_id === row.folder_id && !m.is_seen)
        .filter((m) => includeRow || m.id !== row.id)
        .map((m) => m.id);
      if (ids.length === 0) return;

      for (const id of ids) updateMailInThreadCaches(qc, id, { is_seen: true });
      if (includeRow) updateMailInCache(qc, row.id, { is_seen: true });
      // What stays unread afterwards is at most the row's own message, read
      // from the cache now rather than from `row` -- the reading pane may
      // have marked it read meanwhile.
      const rowStillUnread = !includeRow && findMailInCache(qc, row.id)?.isSeen === false;
      updateConversationUnread(qc, row.id, () => (rowStillUnread ? 1 : 0));
      updateFolderCounts(qc, row.account_id, row.folder_id, 0, -ids.length);
      try {
        await api.messages.bulkAction(row.account_id, { action: "mark_read", ids });
      } catch (err) {
        pushToast(`Could not mark as read: ${(err as Error).message}`, "error", 0);
      } finally {
        refreshMailViews(qc);
      }
    },
    [qc, pushToast],
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
  // reading pane, bulk toolbar) reads from, so moving it on here reaches all
  // of them: once the open message leaves its folder, nothing keeps acting
  // on it under a reading pane that still shows its old content -- except
  // a reply or forward in progress against its thread, which unmounting
  // the pane would take down too. See activeReplyDirtyForThreadId below.
  //
  // This writes selectedMailIdAtom directly rather than through
  // requestSelectMailAtom (lib/atoms.ts): that atom answers "is some
  // composer dirty at all", which is the wrong question here -- an action
  // taken elsewhere on a message must still go through even while a reply
  // on some unrelated thread sits open, and only the neighbour-selection
  // step below is conditional on the affected thread specifically.
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const activeReplyDirtyForThreadId = useAtomValue(activeReplyDirtyForThreadIdAtom);
  const setExplicitlyUnread = useSetAtom(explicitlyUnreadMailIdAtom);
  const navDirection = useAtomValue(mailNavDirectionAtom);
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
      // Recorded here rather than in each button's own handler, and before
      // the optimistic cache write below, so the reading pane's auto-read
      // effect sees it in the same render as the unread flip that effect
      // reacts to.
      if (act === "mark_unread") setExplicitlyUnread(mailId);
      if (act === "mark_read") setExplicitlyUnread((cur) => (cur === mailId ? null : cur));
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
      // Computed before the optimistic removal below, so the neighbour is
      // read off the list the reader was actually looking at.
      if (wasSelected) setSelectedMailId(neighbourInCache(qc, mailId, navDirection));

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
        // and a conversation row's own unread count are derived values
        // rather than plain field copies, so they stay their own branch
        // below.
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

        if (act === "mark_read" && !mailInfo.isSeen) {
          updateFolderCounts(qc, accountId, mailInfo.folderId, 0, -1);
          updateConversationUnread(qc, mailId, (unread) => unread - 1);
        }
        if (act === "mark_unread" && mailInfo.isSeen) {
          updateFolderCounts(qc, accountId, mailInfo.folderId, 0, 1);
          updateConversationUnread(qc, mailId, (unread) => unread + 1);
        }
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
      qc.invalidateQueries({ queryKey: ["mail"] });
      qc.invalidateQueries({ queryKey: ["thread", mailId] });
      refreshMailViews(qc);
    },
  });

  return mailAction;
}
