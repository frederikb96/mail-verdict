/** TanStack Query hooks for search operations. */

import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SearchField, SearchResult, SearchStrictness } from "@/types/api";

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

export const searchKeys = {
  results: (
    semantic: boolean,
    query: string,
    accountId: string | undefined,
    folderIds: string[] | null,
    fields: SearchField[],
    strictness: SearchStrictness,
  ) =>
    [
      "search",
      semantic ? "semantic" : "fulltext",
      query,
      accountId ?? "all",
      folderIds ?? "all-folders",
      semantic ? strictness : [...fields].sort(),
    ] as const,
};

/**
 * Newest-first (fulltext, ranked by field tier then date) or nearest-first
 * (semantic) search results, paginated the same shape the mail list uses.
 * Folder scoping and, in fulltext mode, field scoping are both enforced
 * server-side -- this hook only forwards the current preferences.
 *
 * "Always newest first, no sort control" governs the fulltext list, not
 * semantic mode's own similarity ranking -- overriding that would remove
 * the entire reason semantic search exists, so it keeps ordering by
 * nearest match regardless of date.
 *
 * Semantic mode has no further pages: the strictness cutoff bounds the
 * result set naturally, so hasNextPage is always false once semantic
 * mode's single page has loaded.
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
}) {
  const { query, accountId, folderIds, fields, semantic, strictness } = params;
  const trimmed = query.trim();
  // An explicitly-cleared folder scope ([] -- see search-prefs.ts) means
  // "search nothing", never "no restriction" -- the server reads an
  // absent folder_ids param as every folder, the opposite of what an
  // empty selection means here. Disabling the query is what keeps that
  // from silently becoming an unscoped search.
  const hasFolderScope = folderIds === null || folderIds.length > 0;

  return useInfiniteQuery({
    queryKey: searchKeys.results(semantic, trimmed, accountId, folderIds, fields, strictness),
    queryFn: async ({ pageParam }): Promise<SearchResultPage> => {
      if (semantic) {
        const r = await api.search.semantic({
          q: trimmed,
          account_id: accountId,
          folder_ids: folderIds ?? undefined,
          strictness,
        });
        return { items: r.results, has_more: false, next_cursor: null, total: r.results.length };
      }
      const r = await api.search.query({
        q: trimmed,
        account_id: accountId,
        folder_ids: folderIds ?? undefined,
        fields,
        before: pageParam ?? undefined,
        limit: 50,
      });
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
