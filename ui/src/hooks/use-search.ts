/** TanStack Query hooks for search operations. */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SearchField, SearchResult, SearchSort, SearchStrictness } from "@/types/api";

/** The fulltext and semantic endpoints already return the same shape --
 * MessageSummary plus how the query matched (match_tier for fulltext,
 * similarity for semantic) -- so the page and its row renderer consume
 * SearchResult directly, with no separate per-mode mapping to reconcile. */
export type SearchResultItem = SearchResult;

interface SearchResultPage {
  items: SearchResultItem[];
  has_more: boolean;
  next_cursor: string | null;
  total: number;
}

/** A stretch of received_at, both ends optional -- the same shape
 * search-prefs.ts's SearchDateRange persists, kept separate here so this
 * hook doesn't depend on that module's own atom type. */
export interface SearchDateRangeParam {
  after?: string;
  before?: string;
}

export const searchKeys = {
  results: (
    semantic: boolean,
    query: string,
    accountId: string | undefined,
    folderIds: string[] | null,
    fields: SearchField[],
    strictness: SearchStrictness,
    sort: SearchSort,
    dateRange: SearchDateRangeParam | undefined,
    isSeen?: boolean,
  ) =>
    [
      "search",
      semantic ? "semantic" : "fulltext",
      query,
      accountId ?? "all",
      folderIds ?? "all-folders",
      semantic ? strictness : [...fields].sort(),
      sort,
      dateRange?.after ?? "", dateRange?.before ?? "",
      isSeen === undefined ? "any" : isSeen ? "read" : "unread",
    ] as const,
  dateBounds: (accountId: string | undefined, folderIds: string[] | null) =>
    ["search", "date-bounds", accountId ?? "all", folderIds ?? "all-folders"] as const,
};

/**
 * Ranked by field tier then newest, or by nearest match for semantic
 * search -- both the default ("relevance") -- or by date alone
 * ("chronological", tier/distance ignored entirely once a result has
 * cleared semantic's own strictness cutoff). Paginated the same shape the
 * mail list uses. Folder scoping, field scoping (fulltext) and the
 * received_at range are all enforced server-side -- this hook only
 * forwards the current preferences.
 *
 * Semantic mode has no further pages: the strictness cutoff bounds the
 * result set naturally, so hasNextPage is always false once semantic
 * mode's single page has loaded. A cursor is never reused across a sort
 * switch -- like every other control here, changing it restarts
 * pagination from scratch (the query key includes it).
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
  semantic: boolean;
  strictness: SearchStrictness;
  sort?: SearchSort;
  dateRange?: SearchDateRangeParam;
  /** Only read (true) or only unread (false) mail -- text search only. */
  isSeen?: boolean;
}) {
  const {
    query, accountId, folderIds, fields, semantic, strictness,
    sort = "relevance", dateRange, isSeen,
  } = params;
  const trimmed = query.trim();
  // An explicitly-cleared folder scope ([] -- see search-prefs.ts) means
  // "search nothing", never "no restriction" -- the server reads an
  // absent folder_ids param as every folder, the opposite of what an
  // empty selection means here. Disabling the query is what keeps that
  // from silently becoming an unscoped search.
  const hasFolderScope = folderIds === null || folderIds.length > 0;

  return useInfiniteQuery({
    queryKey: searchKeys.results(
      semantic, trimmed, accountId, folderIds, fields, strictness, sort, dateRange, isSeen,
    ),
    queryFn: async ({ pageParam, signal }): Promise<SearchResultPage> => {
      if (semantic) {
        const r = await api.search.semantic({
          q: trimmed,
          account_id: accountId,
          folder_ids: folderIds ?? undefined,
          strictness,
          sort,
          received_after: dateRange?.after,
          received_before: dateRange?.before,
        }, signal);
        return { items: r.results, has_more: false, next_cursor: null, total: r.results.length };
      }
      const r = await api.search.query({
        q: trimmed,
        account_id: accountId,
        folder_ids: folderIds ?? undefined,
        fields,
        before: pageParam ?? undefined,
        limit: 50,
        sort,
        received_after: dateRange?.after,
        received_before: dateRange?.before,
        is_seen: isSeen,
      }, signal);
      return {
        items: r.results,
        has_more: r.has_more,
        next_cursor: r.next_cursor,
        total: r.total,
      };
    },
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : undefined),
    enabled: trimmed.length >= 2 && hasFolderScope,
    staleTime: 30_000,
  });
}

/** The date-range control's own axis -- oldest/newest received_at across
 * a scope, independent of any query text. */
export function useSearchDateBounds(accountId: string | undefined, folderIds: string[] | null) {
  return useQuery({
    queryKey: searchKeys.dateBounds(accountId, folderIds),
    queryFn: () => api.search.dateBounds({ account_id: accountId, folder_ids: folderIds ?? undefined }),
    staleTime: 60_000,
  });
}
