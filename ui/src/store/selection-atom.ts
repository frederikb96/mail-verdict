/** Jotai atoms for client-held mail selection state. See lib/selection.ts
 * for the state shape and the pure functions that mutate it. */

import { atom } from "jotai";
import {
  isUnifiedViewAtom,
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedUnifiedFolderAtom,
  threadedViewAtom,
} from "@/lib/atoms";
import {
  EMPTY_SELECTION,
  selectionForScope,
  selectionSize,
  type SelectionScope,
  type SelectionState,
} from "@/lib/selection";

/** The whole selection: predicate plus included/excluded ids plus the
 * shift-range anchor. Client-only -- never round-trips to the server
 * except as the scope/ids a bulk-action request resolves server-side.
 *
 * This is the raw value a gesture writes to; nothing should read from it
 * directly to decide what's selected or actionable -- see
 * `effectiveSelectionAtom` below, which is what "is this selection still
 * live" is decided from, once, rather than at each call site. */
export const selectionAtom = atom<SelectionState>(EMPTY_SELECTION);

/** The identity of the list currently on screen -- account, folder (or
 * unified folder name), and threading mode. A selection only ever applies
 * to the list it was made in; this is what `effectiveSelectionAtom`
 * compares it against. */
export const currentListScopeAtom = atom<SelectionScope>((get) => {
  const isUnified = get(isUnifiedViewAtom);
  return {
    accountId: isUnified ? "unified" : get(selectedAccountIdAtom) ?? "",
    folderId: isUnified ? get(selectedUnifiedFolderAtom) ?? "" : get(selectedFolderIdAtom) ?? "",
    threaded: isUnified ? false : get(threadedViewAtom),
  };
});

/** The selection as it applies to the list currently on screen -- empty
 * whenever the account, folder or threading mode has moved on since the
 * selection was made. Reads (count, isSelected, what a bulk action
 * resolves) all go through this, never the raw atom above. */
export const effectiveSelectionAtom = atom<SelectionState>((get) =>
  selectionForScope(get(selectionAtom), get(currentListScopeAtom)),
);

/** Count of selected mails (derived -- see lib/selection.ts). */
export const selectionCountAtom = atom<number>((get) => selectionSize(get(effectiveSelectionAtom)));

/** Whether selection mode is active: explicit ids, or a folder-wide predicate. */
export const selectionModeAtom = atom<boolean>((get) => {
  const s = get(effectiveSelectionAtom);
  return s.predicate !== null || s.included.size > 0;
});
