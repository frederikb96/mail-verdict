/** TanStack Query hooks for search operations. */

import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { readTimeOf, timedRead } from "@/lib/read-clock";
import { resolveWindow, windowKey, type SearchWindow } from "@/lib/search-window";
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
  results: (
    semantic: boolean,
    query: string,
    accountId: string | undefined,
    folderIds: string[] | null,
    fields: SearchField[],
    window: SearchWindow | null,
    isSeen?: boolean,
  ) =>
    [
      "search",
      semantic ? "semantic" : "fulltext",
      query,
      accountId ?? "all",
      folderIds ?? "all-folders",
      semantic ? "" : [...fields].sort(),
      windowKey(window),
      isSeen === undefined ? "any" : isSeen ? "read" : "unread",
    ] as const,
};

/**
 * Fulltext hits arrive ranked by field tier then newest; semantic hits in
 * two tiers (likely, less likely), each newest first. Paginated the same
 * shape the mail list uses. Folder scoping, field scoping (fulltext) and
 * the time window are all enforced server-side -- this hook only forwards
 * the current preferences, resolving the window against the clock each
 * time a page is read so a preset like "Last 7 days" stays rolling.
 *
 * Semantic mode has no further pages: the tiering bounds the result set
 * naturally, so hasNextPage is always false once semantic mode's single
 * page has loaded.
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
  window?: SearchWindow | null;
  /** Only read (true) or only unread (false) mail -- text search only. */
  isSeen?: boolean;
}) {
  const { query, accountId, folderIds, fields, semantic, window = null, isSeen } = params;
  const trimmed = query.trim();
  // An explicitly-cleared folder scope ([] -- see search-prefs.ts) means
  // "search nothing", never "no restriction" -- the server reads an
  // absent folder_ids param as every folder, the opposite of what an
  // empty selection means here. Disabling the query is what keeps that
  // from silently becoming an unscoped search.
  const hasFolderScope = folderIds === null || folderIds.length > 0;

  const queryKey = searchKeys.results(
    semantic, trimmed, accountId, folderIds, fields, window, isSeen,
  );
  const results = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam, signal }): Promise<SearchResultPage> =>
      timedRead(queryKey, signal, pageParam !== null, () => readPage(pageParam, signal)),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : undefined),
    enabled: trimmed.length >= 2 && hasFolderScope,
    staleTime: 30_000,
  });
  // When the results were read, for the mail actions shown over them.
  return { ...results, readAt: readTimeOf(queryKey, results.dataUpdatedAt) };

  async function readPage(pageParam: string | null, signal: AbortSignal): Promise<SearchResultPage> {
    const range = window ? resolveWindow(window, new Date()) : undefined;
    if (semantic) {
      const r = await api.search.semantic({
        q: trimmed,
        account_id: accountId,
        folder_ids: folderIds ?? undefined,
        received_after: range?.after,
        received_before: range?.before,
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
