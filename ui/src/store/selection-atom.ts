/** Jotai atoms for client-held mail selection state. */

import { atom } from "jotai";

/** Set of selected mail IDs. Client-only — never round-trips to the server. */
export const selectedMailIdsAtom = atom<Set<string>>(new Set<string>());

/** Count of selected mails (derived). */
export const selectionCountAtom = atom<number>(
  (get) => get(selectedMailIdsAtom).size,
);

/** Last clicked mail ID for shift-select anchor tracking. */
export const lastClickedMailIdAtom = atom<string | null>(null);

/**
 * Set when "select all" is used on a folder larger than what is fetched
 * client-side. Bulk actions send this scope instead of an id list; any
 * subsequent per-mail toggle clears it back to an explicit id set.
 */
export const selectionScopeAtom = atom<{
  folderId: string;
  filter?: "unread" | "all";
} | null>(null);

/** Whether selection mode is active: explicit ids, or a folder-wide scope. */
export const selectionModeAtom = atom<boolean>(
  (get) => get(selectedMailIdsAtom).size > 0 || get(selectionScopeAtom) !== null,
);
