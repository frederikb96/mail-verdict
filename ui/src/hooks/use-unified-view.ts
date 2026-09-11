/**
 * Hooks for unified views: the views themselves, a view's mail, and the
 * writes that manage them.
 *
 * Shared between web and React Native.
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { invalidateMailListsBounded, mailListQueryOptions } from "@/hooks/use-mails";
import type { UnifiedFolderResponse } from "@/types/api";

export const unifiedKeys = {
  folders: ["unified", "folders"] as const,
  order: ["unified", "folder-order"] as const,
  mails: (folderName?: string, threaded?: boolean, aroundId?: string, unreadOnly?: boolean) =>
    [
      "unified", "mails", folderName, threaded ? "threaded" : "flat",
      unreadOnly ? "unread" : undefined, aroundId,
    ].filter(Boolean) as string[],
};

/** Every unified view, in sidebar order, with its member folders. */
export function useUnifiedFolders() {
  return useQuery<UnifiedFolderResponse[]>({
    queryKey: unifiedKeys.folders,
    queryFn: () => api.unified.folders(),
  });
}

/** One view's mail -- paged, threaded, filtered and centred on a message
 * exactly as a folder's is (see mailListQueryOptions). */
export function useUnifiedMails(
  folderName: string | null,
  threaded: boolean,
  aroundId?: string | null,
  unreadOnly = false,
) {
  return useInfiniteQuery(
    mailListQueryOptions(
      unifiedKeys.mails(folderName ?? undefined, threaded, aroundId ?? undefined, unreadOnly),
      (cursor) =>
        api.unified.mails({
          folder_name: folderName!, threaded,
          is_seen: unreadOnly ? false : undefined, limit: 50, ...cursor,
        }),
      aroundId,
      !!folderName,
    ),
  );
}

/** Fetch unified folder display order. */
export function useUnifiedFolderOrder() {
  return useQuery({
    queryKey: unifiedKeys.order,
    queryFn: () => api.unified.getFolderOrder(),
  });
}

/** After any write to a view or a membership: the views, their order, each
 * folder's own list of views, and -- bounded, like every other list refresh
 * -- whatever mail lists a membership change reshapes. */
function useInvalidateUnifiedViews() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: unifiedKeys.folders });
    qc.invalidateQueries({ queryKey: unifiedKeys.order });
    qc.invalidateQueries({ queryKey: ["folders"] });
    invalidateMailListsBounded(qc);
  };
}

/** Mutation to save unified folder display order. */
export function useUpdateUnifiedFolderOrder() {
  const invalidate = useInvalidateUnifiedViews();
  return useMutation({
    mutationFn: (order: string[]) => api.unified.setFolderOrder(order),
    onSuccess: invalidate,
  });
}

export function useCreateUnifiedView() {
  const invalidate = useInvalidateUnifiedViews();
  return useMutation({
    mutationFn: (data: { name: string; emoji?: string | null }) => api.unified.createView(data),
    onSuccess: invalidate,
  });
}

export function useUpdateUnifiedView() {
  const invalidate = useInvalidateUnifiedViews();
  return useMutation({
    mutationFn: ({ viewId, data }: { viewId: string; data: { name?: string; emoji?: string | null } }) =>
      api.unified.updateView(viewId, data),
    onSuccess: invalidate,
  });
}

export function useDeleteUnifiedView() {
  const invalidate = useInvalidateUnifiedViews();
  return useMutation({
    mutationFn: (viewId: string) => api.unified.deleteView(viewId),
    onSuccess: invalidate,
  });
}

/** Make viewIds the complete set of views a folder belongs to. */
export function useSetFolderViews() {
  const invalidate = useInvalidateUnifiedViews();
  return useMutation({
    mutationFn: ({ folderId, viewIds }: { folderId: string; viewIds: string[] }) =>
      api.folders.updatePrefs(folderId, { unified_view_ids: viewIds }),
    onSuccess: invalidate,
  });
}
