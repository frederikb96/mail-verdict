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
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { api } from "@/lib/api";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import { openMailInCache, useMailAction } from "@/hooks/use-mail-intents";
import { useToast } from "@/hooks/use-toast";
import { ACTION_LABELS } from "@/lib/mail-intents";
import { activeReplyDirtyForThreadIdAtom, selectedMailIdAtom } from "@/lib/atoms";
import {
  EMPTY_SELECTION,
  extendRange,
  isRowSelected,
  toggleRow,
  type SelectableRow,
  type SelectionPredicate,
  type SelectionState,
} from "@/lib/selection";
import {
  currentListScopeAtom,
  effectiveSelectionAtom,
  selectionAtom,
  selectionCountAtom,
} from "@/store/selection-atom";
import type {
  BulkActionScope,
  BulkActionTarget,
  BulkActionType,
} from "@/types/api";

/** Read the selection as it applies to the list currently on screen, and
 * whether a given row is ticked. Never the raw atom -- see
 * effectiveSelectionAtom. */
export function useSelection() {
  const state = useAtomValue(effectiveSelectionAtom);
  const count = useAtomValue(selectionCountAtom);
  const isSelected = useCallback((row: SelectableRow) => isRowSelected(state, row), [state]);
  return { state, count, isSelected };
}

/** The gestures a row's checkbox, ctrl+click and shift+click drive. Each
 * one carries the list it's happening in, so a selection made in a folder
 * the reader has since left is discarded rather than extended -- see
 * selectionForScope. */
export function useSelectionGestures() {
  const setState = useSetAtom(selectionAtom);
  const scope = useAtomValue(currentListScopeAtom);

  const toggle = useCallback(
    (row: SelectableRow) => setState((s) => toggleRow(s, row, scope)),
    [setState, scope],
  );

  const shiftRange = useCallback(
    (visibleIds: string[], rowsById: ReadonlyMap<string, SelectableRow>, targetId: string) =>
      setState((s) => extendRange(s, visibleIds, rowsById, targetId, scope)),
    [setState, scope],
  );

  return { toggle, shiftRange };
}

/** Mint a "select all matching" predicate over a whole folder, and clear it.
 * `accountId`/`folderId` are the caller's own current values (always a real
 * account, never the unified view -- see canOfferFolder in
 * selection-banner.tsx), so the predicate's scope is recorded directly from
 * them rather than re-read from elsewhere.
 *
 * `threaded` is the *display* mode the predicate is being minted from, not
 * a property of the predicate itself -- the predicate always matches raw
 * messages in the folder, threaded or not, the same as a bulk action
 * always resolves to messages. Selecting "everything" in a threaded folder
 * therefore captures every message in every matching conversation, not
 * only the latest-per-thread row shown -- an action like Archive removes
 * the whole conversation from the folder, not just its representative row.
 * It has to be recorded in `scope` regardless, or the very next render's
 * `currentListScopeAtom` (which reflects the real display mode) reads as a
 * mismatch against a hardcoded `false` and the predicate is discarded on
 * the spot. */
export function useSelectAll() {
  const setState = useSetAtom(selectionAtom);

  const selectFolderScope = useCallback(
    async (accountId: string, folderId: string, filter: "all" | "unread", threaded: boolean) => {
      const snapshot = await api.messages.selection(accountId, { folder_id: folderId, filter });
      const predicate: SelectionPredicate = {
        accountId, folderId, filter,
        snapshotAt: snapshot.snapshot_at,
        count: snapshot.count,
      };
      setState({ ...EMPTY_SELECTION, predicate, scope: { accountId, folderId, threaded } });
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
 * Acts on an entire folder from the sidebar's own hover menu -- resolves a
 * predicate snapshot server-side in the same request, independent of
 * whatever row selection (if any) is active elsewhere. Never touches the
 * shared selection atom.
 */
export function useFolderBulkAction() {
  const qc = useQueryClient();

  return useMutation({
    mutationFn: async ({
      accountId, folderId, action, confirmedSnapshot,
    }: {
      accountId: string;
      folderId: string;
      action: Extract<BulkActionType, "mark_read" | "expunge">;
      /** A snapshot already minted (and its count already shown to the
       * user) before this ran -- required for expunge, whose destructive
       * confirmation dialog would otherwise show one count while a fresh
       * mint taken only now silently acts on however many actually
       * resolve. mark_read has no confirmation step and mints its own,
       * fresh, every time. */
      confirmedSnapshot?: { snapshotAt: string; count: number };
    }) => {
      const snapshot = confirmedSnapshot
        ? { snapshot_at: confirmedSnapshot.snapshotAt, count: confirmedSnapshot.count }
        : await api.messages.selection(accountId, { folder_id: folderId, filter: "all" });
      return api.messages.bulkAction(accountId, {
        action,
        scope: { folder_id: folderId, filter: "all", snapshot_at: snapshot.snapshot_at },
        confirm_message_count: confirmedSnapshot ? snapshot.count : undefined,
      });
    },
    onSettled: () => {
      // resetQueries, not invalidateQueries: this can touch a whole large
      // folder, and an infinite query's invalidate-refetch replays every
      // already-loaded page sequentially with no cap -- reset instead
      // wipes the cached pages and, for whatever's still observed, fetches
      // page one only. The reader's scroll position is lost, which after a
      // whole-folder action is the honest outcome: the list is either
      // empty or unrecognisably different from what they were looking at.
      qc.resetQueries({ queryKey: ["mails"] });
      invalidateAllFolderCaches(qc);
    },
  });
}

/** Build the request bodies a bulk action sends -- one per affected
 * account, since the API is scoped to a single account per request. A
 * predicate selection is always single-account by construction (minted
 * over one account's folder); an explicit-id selection may span several in
 * the unified view, so it is grouped by the account each id was ticked
 * under -- carried in the selection itself, never re-derived from a list
 * cache that may have since evicted or moved the row. */
function buildBulkRequests(
  state: SelectionState,
): Array<{ accountId: string; target: BulkActionTarget }> {
  if (state.predicate) {
    const scope: BulkActionScope = {
      folder_id: state.predicate.folderId,
      filter: state.predicate.filter,
      snapshot_at: state.predicate.snapshotAt,
      exclude_ids: Array.from(state.excluded.keys()),
    };
    const target: BulkActionTarget = { scope };
    if (state.included.size > 0) target.ids = Array.from(state.included.keys());
    return [{ accountId: state.predicate.accountId, target }];
  }
  const grouped = new Map<string, string[]>();
  for (const [id, accountId] of state.included) {
    const bucket = grouped.get(accountId) ?? [];
    bucket.push(id);
    grouped.set(accountId, bucket);
  }
  // A ticked row in a list grouped by conversation is the whole
  // conversation, so an action on it leaves nothing of it behind.
  const expandThreads = state.scope?.threaded === true;
  return Array.from(grouped.entries()).map(([accountId, ids]) => ({
    accountId, target: expandThreads ? { ids, expand_threads: true } : { ids },
  }));
}

interface ScopeActionVars {
  action: BulkActionType;
  requests: Array<{ accountId: string; target: BulkActionTarget; targetFolderId?: string }>;
}

/** Execute a bulk action on the current selection (ids, scope, or both).
 * Reads the *effective* selection -- one no longer scoped to the list on
 * screen resolves to nothing to act on, rather than to whatever list it
 * was made in.
 *
 * An explicit-id selection becomes mail intents like any single action
 * (use-mail-intents.ts): shown at once, sent and retried in the background,
 * undoable. A predicate selection ("everything in this folder") stays a
 * request the server resolves -- nothing client-side knows which messages
 * it covers, so there is nothing to show ahead of it or to undo. */
export function useBulkAction() {
  const qc = useQueryClient();
  const state = useAtomValue(effectiveSelectionAtom);
  const clearSelection = useClearSelection();
  const { performAll } = useMailAction();
  // A predicate action that carries the open message out of its folder
  // must not leave the reading pane pointed at it -- except when a reply
  // or forward against that message's own thread is still dirty.
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const activeReplyDirtyForThreadId = useAtomValue(activeReplyDirtyForThreadIdAtom);
  const { push: pushToast } = useToast();

  const scopeMutation = useMutation({
    mutationFn: async ({ action, requests }: ScopeActionVars) => {
      const results = await Promise.all(
        requests.map(({ accountId, target, targetFolderId }) =>
          api.messages.bulkAction(accountId, { action, target_folder_id: targetFolderId, ...target }),
        ),
      );
      // The endpoint answers 200 even when it did nothing, carrying the
      // reason in `errors` -- throw so this reaches onError.
      if (!results.every((r) => r.success)) {
        throw new Error(results.flatMap((r) => r.errors).join("; ") || `Could not ${action}`);
      }
      return results;
    },

    onMutate: ({ action }) => {
      const removesFromList = ["move", "trash", "expunge", "archive", "spam"].includes(action);
      const openMail = selectedMailId ? openMailInCache(qc, selectedMailId) : null;
      const hasDirtyReply = openMail != null && openMail.threadId === activeReplyDirtyForThreadId;
      if (
        removesFromList && !hasDirtyReply && state.predicate &&
        openMail?.folderId === state.predicate.folderId
      ) {
        setSelectedMailId(null);
      }
    },

    onError: (err, vars) => {
      const label = ACTION_LABELS[vars.action] ?? vars.action;
      pushToast(`Could not ${label}: ${err.message}`, "error", 0);
    },

    onSettled: () => {
      clearSelection();
      // resetQueries, not invalidateQueries -- a predicate selection can
      // span a whole large folder, and an infinite query's invalidate
      // replays every already-loaded page sequentially with no cap. Reset
      // wipes the cached pages and fetches page one only for whatever's
      // still observed; losing scroll position is the honest outcome for
      // an action that just changed the folder wholesale.
      qc.resetQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["mail"] });
      invalidateAllFolderCaches(qc);
    },
  });

  // `targetFolderId` may be a per-account resolver rather than one shared
  // id, for a unified-view move where each account has its own id for
  // "the same" folder.
  const mutate = useCallback(
    (vars: {
      action: BulkActionType;
      targetFolderId?: string | ((accountId: string) => string | undefined);
    }) => {
      const requests = buildBulkRequests(state).map((r) => ({
        ...r,
        targetFolderId:
          typeof vars.targetFolderId === "function"
            ? vars.targetFolderId(r.accountId)
            : vars.targetFolderId,
      }));
      if (state.predicate) {
        scopeMutation.mutate({ action: vars.action, requests });
        return;
      }
      performAll(
        requests.map(({ accountId, target, targetFolderId }) => ({
          accountId,
          mailIds: target.ids ?? [],
          action: vars.action,
          targetFolderId,
          bulk: true,
          expandThreads: target.expand_threads,
        })),
      );
      clearSelection();
    },
    [state, scopeMutation, performAll, clearSelection],
  );

  return { mutate, isPending: scopeMutation.isPending };
}
