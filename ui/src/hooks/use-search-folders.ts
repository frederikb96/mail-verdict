/**
 * Every real folder available to the search page's folder picker, grouped
 * by account -- as opposed to useUnifiedFolders(), which only returns
 * folders an account has opted into a cross-account grouping
 * (folder_prefs.unified_name set); a folder picker that must default to
 * "every folder" cannot use a source that silently omits ungrouped ones.
 */

import { useQueries } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { folderKeys } from "@/hooks/use-folders";
import { useAccounts } from "@/hooks/use-accounts";
import type { FolderResponse } from "@/types/api";

export interface SearchFolderOption {
  folder: FolderResponse;
  accountId: string;
  accountName: string;
}

export function useSearchFolders(): {
  options: SearchFolderOption[];
  isLoading: boolean;
} {
  const { data: accounts, isLoading: accountsLoading } = useAccounts();
  const activeAccounts = (accounts ?? []).filter((a) => a.is_active);

  const results = useQueries({
    queries: activeAccounts.map((account) => ({
      queryKey: folderKeys.list(account.id),
      queryFn: () => api.folders.list(account.id),
      staleTime: 5_000,
    })),
  });

  const isLoading = accountsLoading || results.some((r) => r.isLoading);
  const options: SearchFolderOption[] = activeAccounts.flatMap((account, i) => {
    const folders = results[i]?.data ?? [];
    return folders.map((folder) => ({
      folder,
      accountId: account.id,
      accountName: account.name,
    }));
  });

  return { options, isLoading };
}
