/** Jotai atoms for client-held mail selection state. See lib/selection.ts
 * for the state shape and the pure functions that mutate it. */

import { atom } from "jotai";
import { EMPTY_SELECTION, selectionSize, type SelectionState } from "@/lib/selection";

/** The whole selection: predicate plus included/excluded ids plus the
 * shift-range anchor. Client-only -- never round-trips to the server
 * except as the scope/ids a bulk-action request resolves server-side. */
export const selectionAtom = atom<SelectionState>(EMPTY_SELECTION);

/** Count of selected mails (derived -- see lib/selection.ts). */
export const selectionCountAtom = atom<number>((get) => selectionSize(get(selectionAtom)));

/** Whether selection mode is active: explicit ids, or a folder-wide predicate. */
export const selectionModeAtom = atom<boolean>((get) => {
  const s = get(selectionAtom);
  return s.predicate !== null || s.included.size > 0;
});
