/**
 * Client-held mail selection and the bulk action that consumes it.
 *
 * Selection lives entirely in Jotai — there is no server-side selection
 * state and no network round-trip per checkbox click. A bulk action sends
 * either the selected id list or a scope descriptor (for "select all" over
 * a folder larger than what is fetched client-side).
 */

import { useCallback } from "react";
import { type InfiniteData, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { api } from "@/lib/api";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import { ACTION_LABELS, UNDOABLE_ACTIONS, updateFolderCounts } from "@/hooks/use-mails";
import { useToast } from "@/hooks/use-toast";
import { selectedMailIdAtom } from "@/lib/atoms";
import {
  lastClickedMailIdAtom,
  selectedMailIdsAtom,
  selectionCountAtom,
  selectionScopeAtom,
} from "@/store/selection-atom";
import type { BulkActionTarget, BulkActionType, MessageListResponse } from "@/types/api";

/** Bulk phrasing for the success toast an undoable bulk action shows. */
const BULK_UNDO_PHRASING: Record<string, string> = {
  trash: "moved to trash",
  archive: "archived",
  spam: "marked as spam",
};

/** Read current selection state. */
export function useSelection() {
  const selectedIds = useAtomValue(selectedMailIdsAtom);
  const count = useAtomValue(selectionCountAtom);
  return { selectedIds, count };
}

/** Toggle a single mail's selection. Clears any active select-all scope. */
export function useToggleSelection() {
  const setSelectedIds = useSetAtom(selectedMailIdsAtom);
  const setLastClicked = useSetAtom(lastClickedMailIdAtom);
  const setScope = useSetAtom(selectionScopeAtom);

  return useCallback(
    (mailId: string) => {
      setScope(null);
      setSelectedIds((prev) => {
        const next = new Set(prev);
        if (next.has(mailId)) next.delete(mailId);
        else next.add(mailId);
        return next;
      });
      setLastClicked(mailId);
    },
    [setSelectedIds, setLastClicked, setScope],
  );
}

/** Select every mail between the last-clicked anchor and a target (shift-click). */
export function useRangeSelection() {
  const setSelectedIds = useSetAtom(selectedMailIdsAtom);
  const setLastClicked = useSetAtom(lastClickedMailIdAtom);
  const setScope = useSetAtom(selectionScopeAtom);

  return useCallback(
    (visibleIds: string[], fromId: string, toId: string) => {
      const fromIdx = visibleIds.indexOf(fromId);
      const toIdx = visibleIds.indexOf(toId);
      if (fromIdx === -1 || toIdx === -1) return;
      const [start, end] = fromIdx < toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
      const range = visibleIds.slice(start, end + 1);
      setScope(null);
      setSelectedIds((prev) => {
        const next = new Set(prev);
        for (const id of range) next.add(id);
        return next;
      });
      setLastClicked(toId);
    },
    [setSelectedIds, setLastClicked, setScope],
  );
}

/** Select every mail currently fetched into the list, or an entire folder via scope. */
export function useSelectAll() {
  const setSelectedIds = useSetAtom(selectedMailIdsAtom);
  const setScope = useSetAtom(selectionScopeAtom);

  const selectFetched = useCallback(
    (mailIds: string[]) => {
      setScope(null);
      setSelectedIds(new Set(mailIds));
    },
    [setSelectedIds, setScope],
  );

  const selectFolderScope = useCallback(
    (folderId: string, filter?: "unread" | "all") => {
      setSelectedIds(new Set());
      setScope({ folderId, filter });
    },
    [setSelectedIds, setScope],
  );

  return { selectFetched, selectFolderScope };
}

/** Clear the selection and any active select-all scope. */
export function useClearSelection() {
  const setSelectedIds = useSetAtom(selectedMailIdsAtom);
  const setLastClicked = useSetAtom(lastClickedMailIdAtom);
  const setScope = useSetAtom(selectionScopeAtom);

  return useCallback(() => {
    setSelectedIds(new Set());
    setLastClicked(null);
    setScope(null);
  }, [setSelectedIds, setLastClicked, setScope]);
}

/** Execute a bulk action on the current selection (ids or scope). */
export function useBulkAction() {
  const qc = useQueryClient();
  const selectedIds = useAtomValue(selectedMailIdsAtom);
  const scope = useAtomValue(selectionScopeAtom);
  const clearSelection = useClearSelection();
  // Same reasoning as useMailAction: a bulk action that carries the open
  // message out of its folder must not leave the reading pane pointed at it.
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const { push: pushToast } = useToast();

  return useMutation({
    mutationFn: ({
      accountId,
      action,
      targetFolderId,
    }: {
      accountId: string;
      action: BulkActionType;
      targetFolderId?: string;
    }) => {
      const target: BulkActionTarget = scope
        ? { scope: { folder_id: scope.folderId, filter: scope.filter } }
        : { ids: Array.from(selectedIds) };
      return api.messages.bulkAction(accountId, {
        action,
        target_folder_id: targetFolderId,
        ...target,
      });
    },

    onMutate: async ({ accountId, action }) => {
      await qc.cancelQueries({ queryKey: ["mails"] });

      const prevMailQueries = qc.getQueriesData({ queryKey: ["mails"] });
      const prevFolders = qc.getQueryData(["folders", accountId]);
      const prevFolderOrder = qc.getQueryData(["folder-order", accountId]);

      // A scope-based action doesn't know which ids are affected client-side;
      // only optimistically update the explicit-id case, invalidate for scope.
      const removesFromList = ["move", "trash", "expunge", "archive", "spam"].includes(action);

      const wasSelected = removesFromList && selectedMailId != null && (
        scope
          ? qc.getQueryData<{ folder_id?: string }>(["mail", selectedMailId])?.folder_id ===
            scope.folderId
          : selectedIds.has(selectedMailId)
      );
      if (wasSelected) setSelectedMailId(null);

      // Captured alongside folderCounts so an undoable action can move each
      // id straight back to the folder it came from -- only meaningful for
      // the explicit-id case: a scope can span far more messages than are
      // loaded client-side, so there is nothing here to reconstruct an undo
      // from.
      const mailIdsByFolder = new Map<string, string[]>();

      if (removesFromList && !scope) {
        const folderCounts = new Map<string, { total: number; unread: number }>();

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
                  const counts = folderCounts.get(m.folder_id) ?? { total: 0, unread: 0 };
                  counts.total++;
                  if (!m.is_seen) counts.unread++;
                  folderCounts.set(m.folder_id, counts);
                  const ids = mailIdsByFolder.get(m.folder_id) ?? [];
                  ids.push(m.id);
                  mailIdsByFolder.set(m.folder_id, ids);
                  return false;
                }),
              })),
            };
          },
        );

        for (const [folderId, counts] of folderCounts) {
          updateFolderCounts(qc, accountId, folderId, -counts.total, -counts.unread);
        }
      }

      return {
        prevMailQueries, prevFolders, prevFolderOrder, accountId, wasSelected, selectedMailId,
        mailIdsByFolder,
      };
    },

    onSuccess: (_data, { accountId, action }, ctx) => {
      if (!UNDOABLE_ACTIONS.includes(action) || ctx.mailIdsByFolder.size === 0) return;
      const mailIdsByFolder = ctx.mailIdsByFolder;
      const count = [...mailIdsByFolder.values()].reduce((n, ids) => n + ids.length, 0);
      pushToast(
        `${count} message${count === 1 ? "" : "s"} ${BULK_UNDO_PHRASING[action]}`,
        "success",
        6000,
        {
          label: "Undo",
          onClick: async () => {
            await Promise.all(
              [...mailIdsByFolder.entries()].map(([folderId, ids]) =>
                api.messages.bulkAction(accountId, {
                  action: "move", target_folder_id: folderId, ids,
                }),
              ),
            );
            qc.invalidateQueries({ queryKey: ["mails"] });
            qc.invalidateQueries({ queryKey: ["mail"] });
            invalidateAllFolderCaches(qc);
          },
        },
      );
    },

    onError: (err, vars, ctx) => {
      const label = ACTION_LABELS[vars.action] ?? vars.action;
      pushToast(`Could not ${label}: ${err.message}`, "error", 0);

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
      if (ctx.wasSelected && ctx.selectedMailId) {
        setSelectedMailId(ctx.selectedMailId);
      }
    },

    onSettled: () => {
      clearSelection();
      qc.invalidateQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["mail"] });
      invalidateAllFolderCaches(qc);
    },
  });
}
