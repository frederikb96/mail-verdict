/**
 * TanStack Query hooks for backend-driven mail selection.
 *
 * Selection state lives on the backend (SelectionManager) and is
 * streamed to the frontend via SSE selection.changed events.
 * These hooks provide mutation wrappers for the selection API.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useAtomValue, useSetAtom } from "jotai";
import { api } from "@/lib/api";
import {
  selectedMailIdsAtom,
  selectionCountAtom,
  lastClickedMailIdAtom,
} from "@/store/selection-atom";
import type {
  BulkActionRequest,
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
      setLastClicked(variables.body.mail_id);
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
    onSuccess: () => {
      // Selection is cleared on backend after bulk action
      setSelectedIds(new Set());
      setLastClicked(null);
      qc.invalidateQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["mail"] });
      qc.invalidateQueries({ queryKey: ["folders"] });
    },
  });
}
