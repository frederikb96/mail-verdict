/** TanStack Query hooks for search operations. */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const searchKeys = {
  query: (q: string, mode?: string, accountId?: string) =>
    ["search", q, mode, accountId].filter(Boolean) as string[],
};

export function useSearch(
  query: string,
  mode?: "semantic" | "fulltext",
  accountId?: string,
) {
  return useQuery({
    queryKey: searchKeys.query(query, mode, accountId),
    queryFn: () =>
      api.search.query({
        q: query,
        mode,
        account_id: accountId,
      }),
    enabled: query.length >= 2,
    staleTime: 30_000,
  });
}
