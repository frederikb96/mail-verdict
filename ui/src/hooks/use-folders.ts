/** TanStack Query hooks for folder operations. */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const folderKeys = {
  list: (accountId: string) => ["folders", accountId] as const,
};

export function useFolders(accountId: string | null) {
  return useQuery({
    queryKey: folderKeys.list(accountId!),
    queryFn: () => api.folders.list(accountId!),
    enabled: !!accountId,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });
}
