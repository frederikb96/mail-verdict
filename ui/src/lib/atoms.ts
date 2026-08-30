/** Jotai state atoms. */

import { atom } from "jotai";
import { atomWithStorage } from "jotai/utils";

/**
 * Currently selected account ID.
 * Special value "unified" indicates the unified multi-account view.
 */
export const selectedAccountIdAtom = atom<string | null>(null);

/** Currently selected folder ID (single-account mode). */
export const selectedFolderIdAtom = atom<string | null>(null);

/** Currently selected unified folder name (unified view mode). */
export const selectedUnifiedFolderAtom = atom<string | null>(null);

/** Whether the unified view is active. */
export const isUnifiedViewAtom = atom<boolean>((get) => {
  return get(selectedAccountIdAtom) === "unified";
});

/** Currently selected mail ID */
export const selectedMailIdAtom = atom<string | null>(null);

/** Whether the mail list groups messages into conversations. Defaults on. */
export const threadedViewAtom = atomWithStorage<boolean>(
  "mailverdict:threaded",
  true,
);
