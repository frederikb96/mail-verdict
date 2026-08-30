/** TanStack Query hooks for search operations. */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const searchKeys = {
  query: (q: string, accountId?: string) =>
    ["search", q, accountId].filter(Boolean) as string[],
};

export function useSearch(query: string, accountId?: string) {
  return useQuery({
    queryKey: searchKeys.query(query, accountId),
    queryFn: () => api.search.query({ q: query, account_id: accountId }),
    enabled: query.length >= 2,
    staleTime: 30_000,
  });
}
