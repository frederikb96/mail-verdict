/** Jotai atoms for backend-driven selection state (SSE-updated). */

import { atom } from "jotai";

/** Set of selected mail IDs, updated by SSE selection.changed events. */
export const selectedMailIdsAtom = atom<Set<string>>(new Set<string>());

/** Count of selected mails (derived). */
export const selectionCountAtom = atom<number>(
  (get) => get(selectedMailIdsAtom).size,
);

/** Last clicked mail ID for shift-select anchor tracking. */
export const lastClickedMailIdAtom = atom<string | null>(null);

/** Whether selection mode is active (at least one mail selected). */
export const selectionModeAtom = atom<boolean>(
  (get) => get(selectedMailIdsAtom).size > 0,
);
