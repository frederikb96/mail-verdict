/**
 * TanStack Query hooks for backend-driven mail selection.
 *
 * Selection state lives on the backend (SelectionManager) and is
 * streamed to the frontend via SSE selection.changed events.
 * These hooks provide mutation wrappers for the selection API.
 */

import { type InfiniteData, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAtomValue, useSetAtom } from "jotai";
import { api } from "@/lib/api";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import { updateFolderCounts } from "@/hooks/use-mails";
import {
  selectedMailIdsAtom,
  selectionCountAtom,
  lastClickedMailIdAtom,
} from "@/store/selection-atom";
import type {
  BulkActionRequest,
  MessageListResponse,
  SelectionAll,
  SelectionRange,
  SelectionToggle,
} from "@/types/api";

/** Read current selection state from Jotai atom (SSE-driven). */
export function useSelection() {
  const selectedIds = useAtomValue(selectedMailIdsAtom);
  const count = useAtomValue(selectionCountAtom);
  return { selectedIds, count };
}

/** Read/write last clicked mail ID for shift-select anchor. */
export function useLastClicked() {
  return useSetAtom(lastClickedMailIdAtom);
}

/** Toggle a single mail's selection. */
export function useToggleSelection() {
  const setLastClicked = useSetAtom(lastClickedMailIdAtom);
  const setSelectedIds = useSetAtom(selectedMailIdsAtom);

  return useMutation({
    mutationFn: ({
      accountId,
      body,
    }: {
      accountId: string;
      body: SelectionToggle;
    }) => api.selection.toggle(accountId, body),
    onSuccess: (data, variables) => {
      setSelectedIds(new Set(data.selected_ids));
      setLastClicked(variables.body.message_id);
    },
  });
}

/** Range-select mails between two anchors (shift-click). */
export function useRangeSelection() {
  const setSelectedIds = useSetAtom(selectedMailIdsAtom);

  return useMutation({
    mutationFn: ({
      accountId,
      body,
    }: {
      accountId: string;
      body: SelectionRange;
    }) => api.selection.range(accountId, body),
    onSuccess: (data) => {
      setSelectedIds(new Set(data.selected_ids));
    },
  });
}

/** Select all mails in a folder. */
export function useSelectAll() {
  const setSelectedIds = useSetAtom(selectedMailIdsAtom);

  return useMutation({
    mutationFn: ({
      accountId,
      body,
    }: {
      accountId: string;
      body: SelectionAll;
    }) => api.selection.all(accountId, body),
    onSuccess: (data) => {
      setSelectedIds(new Set(data.selected_ids));
    },
  });
}

/** Clear all selections. */
export function useClearSelection() {
  const setSelectedIds = useSetAtom(selectedMailIdsAtom);
  const setLastClicked = useSetAtom(lastClickedMailIdAtom);

  return useMutation({
    mutationFn: ({ accountId }: { accountId: string }) =>
      api.selection.clear(accountId),
    onSuccess: () => {
      setSelectedIds(new Set());
      setLastClicked(null);
    },
  });
}

/** Execute a bulk action on all selected mails. */
export function useBulkAction() {
  const qc = useQueryClient();
  const selectedIds = useAtomValue(selectedMailIdsAtom);
  const setSelectedIds = useSetAtom(selectedMailIdsAtom);
  const setLastClicked = useSetAtom(lastClickedMailIdAtom);

  return useMutation({
    mutationFn: ({
      accountId,
      body,
    }: {
      accountId: string;
      body: BulkActionRequest;
    }) => api.selection.action(accountId, body),

    onMutate: async ({ accountId, body }) => {
      await qc.cancelQueries({ queryKey: ["mails"] });

      const prevMailQueries = qc.getQueriesData({ queryKey: ["mails"] });
      const prevFolders = qc.getQueryData(["folders", accountId]);
      const prevFolderOrder = qc.getQueryData(["folder-order", accountId]);

      const removesFromList = ["move", "delete", "archive", "spam"].includes(body.action);

      if (removesFromList) {
        // Count unread mails being removed per folder for count adjustments
        const folderUnread = new Map<string, { total: number; unread: number }>();

        qc.setQueriesData<InfiniteData<MessageListResponse>>(
          { queryKey: ["mails"] },
          (old) => {
            if (!old) return old;
            return {
              ...old,
              pages: old.pages.map((page) => ({
                ...page,
                messages: page.messages.filter((m) => {
                  if (!selectedIds.has(m.id)) return true;
                  const key = m.folder_id;
                  const counts = folderUnread.get(key) ?? { total: 0, unread: 0 };
                  counts.total++;
                  if (!m.is_seen) counts.unread++;
                  folderUnread.set(key, counts);
                  return false;
                }),
              })),
            };
          },
        );

        for (const [folderId, counts] of folderUnread) {
          updateFolderCounts(qc, accountId, folderId, -counts.total, -counts.unread);
        }
      }

      return { prevMailQueries, prevFolders, prevFolderOrder, accountId };
    },

    onError: (_err, _vars, ctx) => {
      if (!ctx) return;
      if (ctx.prevMailQueries) {
        for (const [key, data] of ctx.prevMailQueries as Array<[readonly unknown[], unknown]>) {
          qc.setQueryData(key, data);
        }
      }
      if (ctx.prevFolders && ctx.accountId) {
        qc.setQueryData(["folders", ctx.accountId], ctx.prevFolders);
      }
      if (ctx.prevFolderOrder && ctx.accountId) {
        qc.setQueryData(["folder-order", ctx.accountId], ctx.prevFolderOrder);
      }
    },

    onSettled: () => {
      setSelectedIds(new Set());
      setLastClicked(null);
      qc.invalidateQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["mail"] });
      invalidateAllFolderCaches(qc);
    },
  });
}
