/**
 * Hooks for unified views: the views themselves, a view's mail, and the
 * writes that manage them.
 *
 * Shared between web and React Native.
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  type MailListWindow,
  mailListQueryOptions,
  refreshMailViews,
  useRefreshWindowOnMount,
} from "@/hooks/use-mails";
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

/** One view's mail -- paged, threaded, filtered, centred on a message and
 * refreshed as one window exactly as a folder's is (see
 * mailListQueryOptions). */
export function useUnifiedMails(
  folderName: string | null,
  threaded: boolean,
  aroundId?: string | null,
  unreadOnly = false,
) {
  const queryKey = unifiedKeys.mails(
    folderName ?? undefined, threaded, aroundId ?? undefined, unreadOnly,
  );
  const listWindow: MailListWindow | undefined =
    folderName && !aroundId ? { kind: "unified", folderName, threaded, unreadOnly } : undefined;
  const result = useInfiniteQuery(
    mailListQueryOptions(
      queryKey,
      (cursor) =>
        api.unified.mails({
          folder_name: folderName!, threaded,
          is_seen: unreadOnly ? false : undefined, limit: 50, ...cursor,
        }),
      aroundId,
      !!folderName,
      listWindow,
    ),
  );
  useRefreshWindowOnMount(queryKey, listWindow !== undefined);
  return result;
}

/** Fetch unified folder display order. */
export function useUnifiedFolderOrder() {
  return useQuery({
    queryKey: unifiedKeys.order,
    queryFn: () => api.unified.getFolderOrder(),
  });
}

/** After any write to a view or a membership: every folder and view cache,
 * and the open mail lists a membership change reshapes, brought up to date
 * together the way every other mail change is (refreshMailViews). */
function useRefreshAfterViewChange() {
  const qc = useQueryClient();
  return () => refreshMailViews(qc);
}

/** Mutation to save unified folder display order. */
export function useUpdateUnifiedFolderOrder() {
  const refresh = useRefreshAfterViewChange();
  return useMutation({
    mutationFn: (order: string[]) => api.unified.setFolderOrder(order),
    onSuccess: refresh,
  });
}

export function useCreateUnifiedView() {
  const refresh = useRefreshAfterViewChange();
  return useMutation({
    mutationFn: (data: { name: string; emoji?: string | null }) => api.unified.createView(data),
    onSuccess: refresh,
  });
}

export function useUpdateUnifiedView() {
  const refresh = useRefreshAfterViewChange();
  return useMutation({
    mutationFn: ({ viewId, data }: { viewId: string; data: { name?: string; emoji?: string | null } }) =>
      api.unified.updateView(viewId, data),
    onSuccess: refresh,
  });
}

export function useDeleteUnifiedView() {
  const refresh = useRefreshAfterViewChange();
  return useMutation({
    mutationFn: (viewId: string) => api.unified.deleteView(viewId),
    onSuccess: refresh,
  });
}

/** Make viewIds the complete set of views a folder belongs to. */
export function useSetFolderViews() {
  const refresh = useRefreshAfterViewChange();
  return useMutation({
    mutationFn: ({ folderId, viewIds }: { folderId: string; viewIds: string[] }) =>
      api.folders.updatePrefs(folderId, { unified_view_ids: viewIds }),
    onSuccess: refresh,
  });
}
