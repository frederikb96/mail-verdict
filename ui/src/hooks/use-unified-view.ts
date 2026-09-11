/**
 * Hooks for unified view: merged folders and mails across accounts.
 *
 * Shared between web and React Native.
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { type MailListWindow, useRefreshWindowOnMount } from "@/hooks/use-mails";
import type { UnifiedFolderResponse, UnifiedMessageListResponse } from "@/types/api";

/** Fetch merged folder list across all accounts. */
export function useUnifiedFolders() {
  return useQuery<UnifiedFolderResponse[]>({
    queryKey: ["unified", "folders"],
    queryFn: () => api.unified.folders(),
  });
}

/** Fetch paginated merged mails for a unified folder. */
export function useUnifiedMails(folderName: string | null) {
  const queryKey = ["unified", "mails", folderName];
  const listWindow: MailListWindow | undefined = folderName
    ? { kind: "unified", folderName }
    : undefined;
  const result = useInfiniteQuery<UnifiedMessageListResponse>({
    queryKey,
    queryFn: ({ pageParam }) =>
      api.unified.mails({
        folder_name: folderName!,
        before: pageParam as string | undefined,
        limit: 50,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? lastPage.next_cursor ?? undefined : undefined,
    enabled: !!folderName,
    // Refreshed as one window by refreshMailViews/refreshMailLists and on
    // mount (use-mails.ts), the same as a single account's list.
    meta: listWindow ? { mailListWindow: listWindow } : undefined,
    refetchOnWindowFocus: false,
    refetchOnMount: false,
  });
  useRefreshWindowOnMount(queryKey, listWindow !== undefined);
  return result;
}

/** Fetch unified folder display order. */
export function useUnifiedFolderOrder() {
  return useQuery({
    queryKey: ["unified", "folder-order"],
    queryFn: () => api.unified.getFolderOrder(),
  });
}

/** Mutation to save unified folder display order. */
export function useUpdateUnifiedFolderOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (order: string[]) => api.unified.setFolderOrder(order),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["unified", "folders"] });
      queryClient.invalidateQueries({ queryKey: ["unified", "folder-order"] });
    },
  });
}
