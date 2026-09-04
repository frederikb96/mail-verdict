/** TanStack Query hooks for search operations. */

import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SearchField, SearchResult, SemanticSearchResult } from "@/types/api";

/** The fulltext and semantic endpoints return different shapes (one
 * paginated, one a single ranked batch); the page and its row renderer
 * only ever need this common one. similarity is present in semantic mode
 * only. */
export interface SearchResultItem {
  message_id: string;
  account_id: string;
  folder_id: string;
  subject: string | null;
  from_addr: string | null;
  received_at: string | null;
  snippet: string | null;
  is_seen: boolean;
  is_flagged: boolean;
  similarity?: number;
}

interface SearchResultPage {
  items: SearchResultItem[];
  has_more: boolean;
  next_cursor: string | null;
}

function fromFulltext(r: SearchResult): SearchResultItem {
  return { ...r };
}

function fromSemantic(r: SemanticSearchResult): SearchResultItem {
  // No snippet: the semantic endpoint ranks by embedding similarity, not
  // a highlighted excerpt over rendered text.
  return { ...r, snippet: null };
}

export const searchKeys = {
  results: (
    semantic: boolean,
    query: string,
    accountId: string | undefined,
    folderIds: string[] | null,
    fields: SearchField[],
  ) =>
    [
      "search",
      semantic ? "semantic" : "fulltext",
      query,
      accountId ?? "all",
      folderIds ?? "all-folders",
      semantic ? null : [...fields].sort(),
    ] as const,
};

/**
 * Newest-first (fulltext) or nearest-first (semantic) search results,
 * paginated the same shape the mail list uses. Folder scoping and, in
 * fulltext mode, field scoping are both enforced server-side -- this hook
 * only forwards the current preferences and shapes the two response
 * bodies into one common page type.
 *
 * "Always newest first, no sort control" governs the fulltext list, not
 * semantic mode's own similarity ranking -- overriding that would remove
 * the entire reason semantic search exists, so it keeps ordering by
 * nearest match regardless of date.
 *
 * Semantic mode has no further pages: one embedding per message bounds
 * the corpus far below full-mailbox scale, and re-embedding the query
 * text on every scroll tick would be wasteful and slow. hasNextPage is
 * therefore always false once semantic mode's single page has loaded.
 */
export function useSearchResults(params: {
  query: string;
  accountId?: string;
  folderIds: string[] | null;
  fields: SearchField[];
  semantic: boolean;
}) {
  const { query, accountId, folderIds, fields, semantic } = params;
  const trimmed = query.trim();

  return useInfiniteQuery({
    queryKey: searchKeys.results(semantic, trimmed, accountId, folderIds, fields),
    queryFn: async ({ pageParam }): Promise<SearchResultPage> => {
      if (semantic) {
        const r = await api.search.semantic({
          q: trimmed,
          account_id: accountId,
          folder_ids: folderIds ?? undefined,
          limit: 200,
        });
        return { items: r.results.map(fromSemantic), has_more: false, next_cursor: null };
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
        items: r.results.map(fromFulltext),
        has_more: r.has_more,
        next_cursor: r.next_cursor,
      };
    },
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : undefined),
    enabled: trimmed.length >= 2,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });
}
