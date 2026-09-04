/**
 * Client-held mail selection, its gestures, and the bulk action that
 * consumes it.
 *
 * Selection lives entirely in Jotai (`store/selection-atom.ts`) as a
 * predicate plus included/excluded id sets -- see lib/selection.ts for the
 * shape and the pure functions every gesture below is built from. A bulk
 * action sends either the explicit id set or a scope descriptor (for
 * "select all" over a folder larger than what is fetched client-side, or
 * both together when rows have been added on top of a predicate).
 */

import { useCallback } from "react";
import { type InfiniteData, type QueryClient, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { api } from "@/lib/api";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import { ACTION_LABELS, UNDOABLE_ACTIONS, updateFolderCounts } from "@/hooks/use-mails";
import { useToast } from "@/hooks/use-toast";
import { selectedMailIdAtom } from "@/lib/atoms";
import {
  EMPTY_SELECTION,
  extendRange,
  isRowSelected,
  toggleRow,
  type SelectableRow,
  type SelectionPredicate,
  type SelectionState,
} from "@/lib/selection";
import { selectionAtom, selectionCountAtom } from "@/store/selection-atom";
import type {
  BulkActionScope,
  BulkActionTarget,
  BulkActionType,
  MessageListResponse,
} from "@/types/api";

/** Bulk phrasing for the success toast an undoable bulk action shows. */
const BULK_UNDO_PHRASING: Record<string, string> = {
  trash: "moved to trash",
  archive: "archived",
  spam: "marked as spam",
};

/** Read current selection state and whether a given row is ticked. */
export function useSelection() {
  const state = useAtomValue(selectionAtom);
  const count = useAtomValue(selectionCountAtom);
  const isSelected = useCallback((row: SelectableRow) => isRowSelected(state, row), [state]);
  return { state, count, isSelected };
}

/** The gestures a row's checkbox, ctrl+click and shift+click drive. */
export function useSelectionGestures() {
  const [state, setState] = useAtom(selectionAtom);

  const toggle = useCallback(
    (row: SelectableRow) => setState((s) => toggleRow(s, row)),
    [setState],
  );

  const shiftRange = useCallback(
    (visibleIds: string[], rowsById: ReadonlyMap<string, SelectableRow>, targetId: string) =>
      setState((s) => extendRange(s, visibleIds, rowsById, targetId)),
    [setState],
  );

  return { state, toggle, shiftRange };
}

/** Mint a "select all matching" predicate over a whole folder, and clear it. */
export function useSelectAll() {
  const setState = useSetAtom(selectionAtom);

  const selectFolderScope = useCallback(
    async (accountId: string, folderId: string, filter: "all" | "unread", count: number) => {
      const snapshot = await api.messages.selection(accountId, { folder_id: folderId, filter });
      const predicate: SelectionPredicate = {
        accountId, folderId, filter,
        snapshotAt: snapshot.snapshot_at,
        count: snapshot.count,
      };
      setState({ ...EMPTY_SELECTION, predicate });
      return count;
    },
    [setState],
  );

  return { selectFolderScope };
}

/** Clear the selection entirely: predicate, explicit ids, and the anchor. */
export function useClearSelection() {
  const setState = useSetAtom(selectionAtom);
  return useCallback(() => setState(EMPTY_SELECTION), [setState]);
}

/**
 * Acts on an entire folder from the sidebar's own hover menu -- mints a
 * fresh predicate snapshot and resolves it server-side in the same
 * request, independent of whatever row selection (if any) is active
 * elsewhere. Never touches the shared selection atom.
 */
export function useFolderBulkAction() {
  const qc = useQueryClient();

  return useMutation({
    mutationFn: async ({
      accountId, folderId, action,
    }: {
      accountId: string;
      folderId: string;
      action: Extract<BulkActionType, "mark_read" | "expunge">;
    }) => {
      const snapshot = await api.messages.selection(accountId, { folder_id: folderId, filter: "all" });
      return api.messages.bulkAction(accountId, {
        action,
        scope: { folder_id: folderId, filter: "all", snapshot_at: snapshot.snapshot_at },
      });
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["mails"] });
      invalidateAllFolderCaches(qc);
    },
  });
}

/** Find which account a set of mail ids belongs to, from whatever list
 * caches currently hold them -- needed because a unified-view selection
 * can span accounts, and a bulk-action request is scoped to one. */
function groupIdsByAccount(qc: QueryClient, ids: readonly string[]): Map<string, string[]> {
  const byId = new Map<string, string>();
  for (const [, data] of qc.getQueriesData<InfiniteData<{ messages: { id: string; account_id: string }[] }>>(
    { queryKey: ["mails"] },
  )) {
    for (const page of data?.pages ?? []) {
      for (const m of page.messages) byId.set(m.id, m.account_id);
    }
  }
  for (const [, data] of qc.getQueriesData<InfiniteData<{ messages: { id: string; account_id: string }[] }>>(
    { queryKey: ["unified", "mails"] },
  )) {
    for (const page of data?.pages ?? []) {
      for (const m of page.messages) byId.set(m.id, m.account_id);
    }
  }
  const grouped = new Map<string, string[]>();
  for (const id of ids) {
    const accountId = byId.get(id);
    if (!accountId) continue;
    const bucket = grouped.get(accountId) ?? [];
    bucket.push(id);
    grouped.set(accountId, bucket);
  }
  return grouped;
}

/** Build the request bodies a bulk action sends -- one per affected
 * account, since the API is scoped to a single account per request. A
 * predicate selection is always single-account by construction (minted
 * over one account's folder); an explicit-id selection may span several
 * in the unified view, so it is grouped by each id's real account rather
 * than sent under whichever account the UI happens to have selected. */
function buildBulkRequests(
  qc: QueryClient,
  state: SelectionState,
): Array<{ accountId: string; target: BulkActionTarget }> {
  if (state.predicate) {
    const scope: BulkActionScope = {
      folder_id: state.predicate.folderId,
      filter: state.predicate.filter,
      snapshot_at: state.predicate.snapshotAt,
      exclude_ids: Array.from(state.excluded),
    };
    const target: BulkActionTarget = { scope };
    if (state.included.size > 0) target.ids = Array.from(state.included);
    return [{ accountId: state.predicate.accountId, target }];
  }
  const grouped = groupIdsByAccount(qc, Array.from(state.included));
  return Array.from(grouped.entries()).map(([accountId, ids]) => ({
    accountId, target: { ids },
  }));
}

interface BulkActionVars {
  action: BulkActionType;
  /** Resolved synchronously at `.mutate()` call time, before onMutate's
   * optimistic cache strip runs -- resolving this inside mutationFn
   * instead would read the cache *after* the ids it needs have already
   * been removed from it (onMutate always runs first). `targetFolderId`
   * is per-request rather than shared, because a unified-view move can
   * span accounts that each have their own id for "the same" folder. */
  requests: Array<{ accountId: string; target: BulkActionTarget; targetFolderId?: string }>;
}

/** Execute a bulk action on the current selection (ids, scope, or both). */
export function useBulkAction() {
  const qc = useQueryClient();
  const state = useAtomValue(selectionAtom);
  const clearSelection = useClearSelection();
  // Same reasoning as useMailAction: a bulk action that carries the open
  // message out of its folder must not leave the reading pane pointed at it.
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const { push: pushToast } = useToast();

  const mutation = useMutation({
    mutationFn: async ({ action, requests }: BulkActionVars) => {
      if (requests.length === 0) {
        return { success: true, action, affected_count: 0, errors: [] };
      }
      const results = await Promise.all(
        requests.map(({ accountId, target, targetFolderId }) =>
          api.messages.bulkAction(accountId, { action, target_folder_id: targetFolderId, ...target }),
        ),
      );
      const affected_count = results.reduce((n, r) => n + r.affected_count, 0);
      const errors = results.flatMap((r) => r.errors);
      const success = results.every((r) => r.success);
      // The endpoint answers 200 even when it did nothing, carrying the
      // reason in `errors` -- throw so this reaches onError exactly like
      // the single-row action's HTTPException does, rollback included.
      if (!success) {
        throw new Error(errors.join("; ") || `Could not ${action}`);
      }
      return { success, action, affected_count, errors };
    },

    onMutate: async ({ action }) => {
      await qc.cancelQueries({ queryKey: ["mails"] });

      const prevMailQueries = qc.getQueriesData({ queryKey: ["mails"] });
      const prevFolders = qc.getQueriesData({ queryKey: ["folders"] });
      const prevFolderOrder = qc.getQueriesData({ queryKey: ["folder-order"] });

      // A scope-based action doesn't know which ids are affected client-side;
      // only optimistically update the explicit-id case, invalidate for scope.
      const removesFromList = ["move", "trash", "expunge", "archive", "spam"].includes(action);
      const explicitIds = state.predicate ? null : new Set(state.included);

      const wasSelected =
        removesFromList &&
        selectedMailId != null &&
        (state.predicate
          ? qc.getQueryData<{ folder_id?: string }>(["mail", selectedMailId])?.folder_id ===
            state.predicate.folderId
          : explicitIds?.has(selectedMailId));
      if (wasSelected) setSelectedMailId(null);

      // Captured alongside folderCounts so an undoable action can move each
      // id straight back to the folder (and account) it came from -- only
      // meaningful for the explicit-id case: a predicate can span far more
      // messages than are loaded client-side, so there is nothing here to
      // reconstruct an undo from.
      const mailIdsByFolder = new Map<string, Array<{ id: string; accountId: string }>>();

      if (removesFromList && explicitIds) {
        const folderCounts = new Map<string, { total: number; unread: number; accountId: string }>();

        qc.setQueriesData<InfiniteData<MessageListResponse>>(
          { queryKey: ["mails"] },
          (old) => {
            if (!old) return old;
            return {
              ...old,
              pages: old.pages.map((page) => ({
                ...page,
                messages: page.messages.filter((m) => {
                  if (!explicitIds.has(m.id)) return true;
                  const counts =
                    folderCounts.get(m.folder_id) ?? { total: 0, unread: 0, accountId: m.account_id };
                  counts.total++;
                  if (!m.is_seen) counts.unread++;
                  folderCounts.set(m.folder_id, counts);
                  const ids = mailIdsByFolder.get(m.folder_id) ?? [];
                  ids.push({ id: m.id, accountId: m.account_id });
                  mailIdsByFolder.set(m.folder_id, ids);
                  return false;
                }),
              })),
            };
          },
        );

        for (const [folderId, counts] of folderCounts) {
          updateFolderCounts(qc, counts.accountId, folderId, -counts.total, -counts.unread);
        }
      }

      return {
        prevMailQueries, prevFolders, prevFolderOrder, wasSelected, selectedMailId,
        mailIdsByFolder,
      };
    },

    onSuccess: (data, { action }, ctx) => {
      if (!UNDOABLE_ACTIONS.includes(action) || ctx.mailIdsByFolder.size === 0) return;
      const mailIdsByFolder = ctx.mailIdsByFolder;
      const requested = [...mailIdsByFolder.values()].reduce((n, ids) => n + ids.length, 0);
      // affected_count can fall short of what was requested (an id already
      // gone, for instance) without the response counting as a failure --
      // say so rather than reporting the full requested count as done.
      const partial = data.affected_count < requested;
      const message = partial
        ? `${data.affected_count} of ${requested} message${requested === 1 ? "" : "s"} ${BULK_UNDO_PHRASING[action]}`
        : `${requested} message${requested === 1 ? "" : "s"} ${BULK_UNDO_PHRASING[action]}`;
      pushToast(
        message,
        partial ? "warning" : "success",
        6000,
        {
          label: "Undo",
          onClick: async () => {
            // Each source folder's ids grouped by their real account -- a
            // unified-view undo can span accounts the same way the action
            // it reverses could. The account is the one captured when the
            // row left the cache, not re-derived from it -- by now the row
            // is gone from the cache the derivation would read.
            await Promise.all(
              [...mailIdsByFolder.entries()].flatMap(([folderId, entries]) => {
                const byAccount = new Map<string, string[]>();
                for (const { id, accountId } of entries) {
                  const bucket = byAccount.get(accountId) ?? [];
                  bucket.push(id);
                  byAccount.set(accountId, bucket);
                }
                return [...byAccount.entries()].map(([accountId, ids]) =>
                  api.messages.bulkAction(accountId, {
                    action: "move", target_folder_id: folderId, ids,
                  }),
                );
              }),
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
      if (ctx.prevFolders) {
        for (const [key, data] of ctx.prevFolders as Array<[readonly unknown[], unknown]>) {
          qc.setQueryData(key, data);
        }
      }
      if (ctx.prevFolderOrder) {
        for (const [key, data] of ctx.prevFolderOrder as Array<[readonly unknown[], unknown]>) {
          qc.setQueryData(key, data);
        }
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

  // Resolves `requests` here, synchronously, before onMutate's optimistic
  // cache strip can run -- see BulkActionVars. `targetFolderId` may be a
  // per-account resolver rather than one shared id, for a unified-view
  // move where each account has its own id for "the same" folder.
  const mutate = useCallback(
    (vars: {
      action: BulkActionType;
      targetFolderId?: string | ((accountId: string) => string | undefined);
    }) => {
      const requests = buildBulkRequests(qc, state).map((r) => ({
        ...r,
        targetFolderId:
          typeof vars.targetFolderId === "function"
            ? vars.targetFolderId(r.accountId)
            : vars.targetFolderId,
      }));
      mutation.mutate({ action: vars.action, requests });
    },
    [mutation, qc, state],
  );

  return { ...mutation, mutate };
}
