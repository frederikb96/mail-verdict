/** TanStack Query hooks for mail operations. */

import { useEffect, useMemo } from "react";
import {
  type InfiniteData,
  type Query,
  type QueryClient,
  infiniteQueryOptions,
  keepPreviousData,
  queryOptions,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import { useProjectionIntents } from "@/hooks/use-intent-ledger";
import {
  type WindowRow,
  chunkIntoPages,
  keptWhileUnreadIds,
  mergeRefreshedWindow,
  windowRefreshLimit,
} from "@/lib/mail-list-window";
import { projectThreadMessages, type RowIntentMarks } from "@/lib/mail-intents";
import { isMailListQuery } from "@/lib/query-persister";
import { readTimeOf, recordRead, timedRead } from "@/lib/read-clock";
import type {
  MessageDetail,
  MessageListResponse,
  MessageQuoteResponse,
  ThreadResponse,
  UnifiedFolderResponse,
} from "@/types/api";

export const mailKeys = {
  list: (
    accountId?: string, folderId?: string, threaded?: boolean, aroundId?: string,
    unreadOnly?: boolean,
  ) =>
    [
      "mails", accountId, folderId, threaded ? "threaded" : "flat",
      unreadOnly ? "unread" : undefined, aroundId,
    ].filter(Boolean) as string[],
  detail: (id: string) => ["mail", id] as const,
  thread: (id: string) => ["thread", id] as const,
  allThreads: () => ["thread"] as const,
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
  | {
      kind: "account"; accountId: string; folderId: string;
      threaded: boolean; unreadOnly: boolean;
    }
  | { kind: "unified"; folderName: string; threaded: boolean; unreadOnly: boolean };

type WindowPage = { messages: WindowRow[]; has_more: boolean };

function windowOf(query: Query): MailListWindow | undefined {
  return query.meta?.mailListWindow as MailListWindow | undefined;
}

async function readWindow(
  source: MailListWindow,
  limit: number,
): Promise<{ rows: WindowRow[]; hasMore: boolean }> {
  const filters = {
    threaded: source.threaded, is_seen: source.unreadOnly ? false : undefined, limit,
  };
  const page =
    source.kind === "account"
      ? await api.mails.list({
          account_id: source.accountId, folder_id: source.folderId, ...filters,
        })
      : await api.unified.mails({ folder_name: source.folderName, ...filters });
  return { rows: page.messages, hasMore: page.has_more };
}

/** Rows back into the page shape a mail list's infinite query holds -- an
 * account's and a unified view's alike -- with the page params its own
 * paging would have produced, so the next page it fetches continues from
 * the right row. */
function windowAsInfiniteData(rows: WindowRow[], hasMore: boolean): InfiniteData<unknown, unknown> {
  const pages = chunkIntoPages(rows);
  const last = pages.length - 1;
  const lastIdOf = (page: WindowRow[]) => page[page.length - 1]?.id ?? null;
  const pageHasMore = (i: number) => (i < last || hasMore) && lastIdOf(pages[i]) !== null;
  const nextCursor = (i: number) => (pageHasMore(i) ? lastIdOf(pages[i]) : null);
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
  // The window is dated from when the read began, not when it landed: a
  // mail action whose request succeeded after this moment may be missing
  // from what comes back, and must keep being shown over it (mail-intents.ts).
  const readStartedAt = Date.now();
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
    // Only an unread-only window's own filter can be why a row the reader
    // is looking at drops out of a fresh read -- an ordinary window has
    // nothing to preserve it against, since a row it no longer returns
    // has genuinely left the folder.
    source.unreadOnly ? keptWhileUnreadIds : undefined,
  );
  qc.setQueryData(query.queryKey, windowAsInfiniteData(merged.rows, merged.hasMore), {
    updatedAt: readStartedAt,
  });
  recordRead(query.queryKey, readStartedAt);
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

/** The folders a mail change touched, or null when that is not known --
 * which refreshes every list. Folder ids are unique across accounts. */
export type MailChangeScope = { folderIds: ReadonlySet<string> } | null;

/** The folders a list shows: its own folder, or a unified view's members as
 * last read. Null when a view's membership is not cached. */
function listFolderIds(qc: QueryClient, query: Query): readonly string[] | null {
  const source = windowOf(query);
  const key = query.queryKey;
  const folderId = source ? (source.kind === "account" ? source.folderId : null) : key[0] === "mails" ? key[2] : null;
  if (typeof folderId === "string") return [folderId];
  const viewName = source?.kind === "unified" ? source.folderName : key[0] === "unified" ? key[2] : null;
  const views = qc.getQueryData<UnifiedFolderResponse[]>(["unified", "folders"]);
  const view = views?.find((v) => v.unified_name === viewName);
  return view ? view.folders.map((f) => f.folder_id) : null;
}

function concerns(qc: QueryClient, query: Query, scope: MailChangeScope): boolean {
  if (scope === null) return true;
  const folders = listFolderIds(qc, query);
  return folders === null || folders.some((id) => scope.folderIds.has(id));
}

/**
 * Bring the open mail lists a change concerns up to date -- the list half
 * of refreshMailViews on its own, for a window regaining focus, where the
 * counts already refetch through their own queries. A list nobody is
 * looking at is refreshed when it is shown again (useRefreshWindowOnMount).
 */
export function refreshMailLists(qc: QueryClient, scope: MailChangeScope = null): void {
  const lists = qc.getQueryCache().findAll({ predicate: (q) => isMailListQuery(q.queryKey) });
  for (const query of lists) {
    if (!concerns(qc, query, scope)) continue;
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
 * can refresh the counts and leave a list behind. Only the lists showing a
 * folder the change touched are re-read; every count is.
 */
export function refreshMailViews(qc: QueryClient, scope: MailChangeScope = null): void {
  refreshMailLists(qc, scope);
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

/** Where in the list one page sits -- the list's own filters are the
 * caller's business, this is only the cursor. */
export type MailListCursor = { before?: string; after?: string; around?: string };

/**
 * The paging every mail list shares: an account's folder (useMailList) and
 * a unified view (useUnifiedMails) page, refetch and centre on a message
 * identically, and differ only in the request one page makes.
 *
 * aroundId centres the *first* fetch of a fresh query key on that message
 * instead of the newest edge -- see mail-list.tsx, which captures it once
 * per list identity rather than re-reading it reactively. The caller folds
 * it into queryKey, so a centred window is a genuinely different cached
 * list from an edge-anchored one over the same messages.
 */
export function mailListQueryOptions(
  queryKey: string[],
  fetchPage: (cursor: MailListCursor) => Promise<MessageListResponse>,
  aroundId: string | null | undefined,
  enabled: boolean,
  listWindow: MailListWindow | undefined,
) {
  return infiniteQueryOptions({
    queryKey,
    queryFn: ({ pageParam, queryKey: key, signal }: {
      pageParam: MailListPageParam; queryKey: readonly unknown[]; signal: AbortSignal;
    }) => {
      // A further page keeps the older read time of the pages before it.
      const continues = pageParam.kind === "before" || pageParam.kind === "after";
      return timedRead(key, signal, continues, () => {
        switch (pageParam.kind) {
          case "around":
            return fetchPage({ around: pageParam.id });
          case "before":
            return fetchPage({ before: pageParam.cursor });
          case "after":
            return fetchPage({ after: pageParam.cursor });
          case "initial":
            return fetchPage({});
        }
      });
    },
    initialPageParam: (
      aroundId ? { kind: "around", id: aroundId } : { kind: "initial" }
    ) as MailListPageParam,
    getNextPageParam: (lastPage): MailListPageParam | undefined =>
      lastPage.has_more ? { kind: "before", cursor: lastPage.next_cursor! } : undefined,
    getPreviousPageParam: (firstPage): MailListPageParam | undefined =>
      firstPage.has_more_newer ? { kind: "after", cursor: firstPage.prev_cursor! } : undefined,
    enabled,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
    // Only a list starting at the newest edge is refreshed as one window
    // (listWindow, which the caller also hands useRefreshWindowOnMount); one
    // opened around a message keeps TanStack's own refetch, bounded.
    meta: listWindow ? { mailListWindow: listWindow } : undefined,
    refetchOnWindowFocus: listWindow ? false : aroundWindowIsShallow,
    refetchOnMount: listWindow ? false : aroundWindowIsShallow,
  });
}

/** An account's folder, newest first; unreadOnly narrows it to unread mail. */
export function useMailList(
  accountId: string | null,
  folderId: string | null,
  threaded: boolean,
  aroundId?: string | null,
  unreadOnly = false,
) {
  const queryKey = mailKeys.list(
    accountId ?? undefined, folderId ?? undefined, threaded, aroundId ?? undefined, unreadOnly,
  );
  const listWindow: MailListWindow | undefined =
    accountId && folderId && !aroundId
      ? { kind: "account", accountId, folderId, threaded, unreadOnly }
      : undefined;
  const query = useInfiniteQuery(
    mailListQueryOptions(
      queryKey,
      (cursor) =>
        api.mails.list({
          account_id: accountId!, folder_id: folderId ?? undefined, threaded,
          is_seen: unreadOnly ? false : undefined, limit: 50, ...cursor,
        }),
      aroundId,
      !!accountId && !!folderId,
      listWindow,
    ),
  );
  useRefreshWindowOnMount(queryKey, listWindow !== undefined);
  return { ...query, readAt: readTimeOf(queryKey, query.dataUpdatedAt) };
}

export function useMailDetail(mailId: string | null) {
  return useQuery({
    queryKey: mailKeys.detail(mailId!),
    queryFn: () => api.mails.get(mailId!),
    enabled: !!mailId,
    staleTime: 5 * 60_000,
  });
}

/**
 * All messages in a mail's conversation across folders, ascending by date --
 * one definition for the reading pane, the list's open-row lookup and the
 * prefetches, so all of them share one cache entry. Refetched on mount only
 * once stale: the app-wide "always" would re-read a conversation opened a
 * moment ago, or prefetched by the very press that opened it.
 */
export function threadQueryOptions(mailId: string | null) {
  return queryOptions<ThreadResponse>({
    queryKey: mailKeys.thread(mailId!),
    queryFn: ({ queryKey, signal }) =>
      timedRead(queryKey, signal, false, () => api.mails.thread(mailId!)),
    enabled: !!mailId,
    staleTime: 30_000,
    refetchOnMount: true,
  });
}

export type ProjectedThread = Omit<ThreadResponse, "messages"> & {
  messages: Array<MessageDetail & RowIntentMarks>;
};

/** A conversation, with every mail action on its messages the server may
 * not have applied yet shown on top (mail-intents.ts). */
export function useThread(mailId: string | null) {
  const query = useQuery(threadQueryOptions(mailId));
  const intents = useProjectionIntents();
  const readAt = readTimeOf(mailKeys.thread(mailId!), query.dataUpdatedAt);
  const data = useMemo((): ProjectedThread | undefined => {
    if (!query.data) return undefined;
    const messages = projectThreadMessages(query.data.messages, intents, readAt);
    return messages === query.data.messages ? query.data : { ...query.data, messages };
  }, [query.data, readAt, intents]);
  return { ...query, data };
}

/** Starts fetching a conversation ahead of its opening; a no-op while fresh. */
export function prefetchThread(qc: QueryClient, mailId: string): void {
  void qc.prefetchQuery(threadQueryOptions(mailId));
}

/**
 * Brings cached conversations up to date after mail changed. A conversation
 * is cached under whichever of its messages opened it, so one holding a
 * changed message is found by its contents rather than its key. mail.new
 * names no thread, so an arrival re-reads the conversation on screen -- a
 * reply landing, or the sent copy of one's own reply reaching Sent; only an
 * observed query refetches, and the reading pane holds at most one.
 */
export function refreshThreads(
  qc: QueryClient,
  changedIds: ReadonlySet<string>,
  arrived: boolean,
): void {
  if (arrived) {
    qc.invalidateQueries({ queryKey: mailKeys.allThreads() });
    return;
  }
  if (changedIds.size === 0) return;
  qc.invalidateQueries({
    queryKey: mailKeys.allThreads(),
    predicate: (query) =>
      changedIds.has(query.queryKey[1] as string) ||
      !!(query.state.data as ThreadResponse | undefined)?.messages.some((m) => changedIds.has(m.id)),
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

/**
 * Update a message's content in every ["thread", *] cache that currently
 * holds it -- a thread is cached under the id it was first opened with,
 * so the same message can appear in more than one such cache (or in
 * none, if its thread was never opened). Content only: flags and folders
 * are never patched into a cache, they are projected from the mail
 * intent ledger (mail-intents.ts).
 */
export function updateMailInThreadCaches(
  qc: QueryClient,
  mailId: string,
  updates: Partial<MessageDetail>,
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
 * "Load for this message": re-fetches one message with the allowlist
 * override on and patches just its body into every ["thread", *] cache
 * holding it. GET /messages/{id}'s own images_allowed keeps reporting the
 * sender's real, unchanged allowlist status -- only body_html and
 * has_blocked_images reflect this one-off load, so the banner hides and
 * the images show without quietly marking the sender trusted.
 */
export function useLoadMessageImages() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (messageId: string) => api.mails.get(messageId, true),
    onSuccess: (detail) => {
      updateMailInThreadCaches(qc, detail.id, {
        body_html: detail.body_html,
        has_blocked_images: detail.has_blocked_images,
      });
    },
  });
}

