/** TanStack Query hooks for search operations. */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { readTimeOf, timedRead } from "@/lib/read-clock";
import { SEMANTIC_UNAVAILABLE_STATUS } from "@/lib/search-view";
import { resolveWindow, windowKey, type SearchWindow } from "@/lib/search-window";
import { withFields } from "@/lib/query-result";
import type { SearchField, SearchResult, SemanticSearchHit } from "@/types/api";

/** The fulltext and semantic endpoints already return the same shape --
 * MessageSummary plus how the query matched (match_tier for fulltext, tier
 * for semantic) -- so the page and its row renderer consume one type, with
 * no separate per-mode mapping to reconcile. */
export type SearchResultItem = SearchResult & Partial<Pick<SemanticSearchHit, "tier">>;

interface SearchResultPage {
  items: SearchResultItem[];
  has_more: boolean;
  next_cursor: string | null;
  total: number;
}

export const searchKeys = {
  text: (
    query: string,
    accountId: string | undefined,
    folderIds: string[] | null,
    fields: SearchField[],
    window: SearchWindow | null,
    isSeen?: boolean,
  ) =>
    [
      "search",
      "fulltext",
      query,
      accountId ?? "all",
      folderIds ?? "all-folders",
      [...fields].sort(),
      windowKey(window),
      isSeen === undefined ? "any" : isSeen ? "read" : "unread",
    ] as const,
  semantic: (
    query: string,
    accountId: string | undefined,
    folderIds: string[] | null,
    window: SearchWindow | null,
  ) =>
    [
      "search", "semantic", query, accountId ?? "all", folderIds ?? "all-folders",
      windowKey(window),
    ] as const,
};

// An explicitly-cleared folder scope ([] -- see search-prefs.ts) means
// "search nothing", never "no restriction" -- the server reads an absent
// folder_ids param as every folder, the opposite of what an empty selection
// means here. Disabling the query is what keeps that from silently becoming
// an unscoped search.
const hasFolderScope = (folderIds: string[] | null) => folderIds === null || folderIds.length > 0;

/**
 * Text search: hits ranked by field tier then newest, paginated the same
 * shape the mail list uses. Folder scoping, field scoping and the time
 * window are all enforced server-side -- this hook only forwards the current
 * preferences, resolving the window against the clock each time a page is
 * read so a preset like "Last 7 days" stays rolling.
 *
 * No placeholderData/keepPreviousData: a new query's results must not be
 * presented as if they were current while the request is in flight --
 * otherwise isLoading never goes true past the very first search of a
 * session, and the spinner that gates on it never appears again.
 */
export function useSearchResults(params: {
  query: string;
  accountId?: string;
  folderIds: string[] | null;
  fields: SearchField[];
  window?: SearchWindow | null;
  /** Only read (true) or only unread (false) mail. */
  isSeen?: boolean;
}) {
  const { query, accountId, folderIds, fields, window = null, isSeen } = params;
  const trimmed = query.trim();

  const queryKey = searchKeys.text(trimmed, accountId, folderIds, fields, window, isSeen);
  const results = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam, signal }): Promise<SearchResultPage> =>
      timedRead(queryKey, signal, pageParam !== null, () => readPage(pageParam, signal)),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : undefined),
    enabled: trimmed.length >= 2 && hasFolderScope(folderIds),
    staleTime: 30_000,
  });
  // When the results were read, for the mail actions shown over them.
  return withFields(results, { readAt: readTimeOf(queryKey, results.dataUpdatedAt) });

  async function readPage(pageParam: string | null, signal: AbortSignal): Promise<SearchResultPage> {
    const range = window ? resolveWindow(window, new Date()) : undefined;
    const r = await api.search.query({
      q: trimmed,
      account_id: accountId,
      folder_ids: folderIds ?? undefined,
      fields,
      before: pageParam ?? undefined,
      limit: 50,
      received_after: range?.after,
      received_before: range?.before,
      is_seen: isSeen,
    }, signal);
    return {
      items: r.results,
      has_more: r.has_more,
      next_cursor: r.next_cursor,
      total: r.total,
    };
  }
}

/**
 * Semantic search: hits in two tiers (likely, less likely), each newest
 * first. A single page -- the tiering bounds the result set. The server
 * answers 503 when it cannot embed the query in time; the query is then not
 * retried, since the page shows the text results instead.
 */
export function useSemanticResults(params: {
  query: string;
  accountId?: string;
  folderIds: string[] | null;
  window?: SearchWindow | null;
  enabled: boolean;
}) {
  const { query, accountId, folderIds, window = null, enabled } = params;
  const trimmed = query.trim();
  const queryKey = searchKeys.semantic(trimmed, accountId, folderIds, window);
  const results = useQuery({
    queryKey,
    queryFn: ({ signal }): Promise<SearchResultItem[]> =>
      timedRead(queryKey, signal, false, async () => {
        const range = window ? resolveWindow(window, new Date()) : undefined;
        const r = await api.search.semantic({
          q: trimmed,
          account_id: accountId,
          folder_ids: folderIds ?? undefined,
          received_after: range?.after,
          received_before: range?.before,
        }, signal);
        return r.results;
      }),
    enabled: enabled && trimmed.length >= 2 && hasFolderScope(folderIds),
    staleTime: 30_000,
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.status === SEMANTIC_UNAVAILABLE_STATUS) &&
      failureCount < 1,
  });
  return withFields(results, { readAt: readTimeOf(queryKey, results.dataUpdatedAt) });
}
