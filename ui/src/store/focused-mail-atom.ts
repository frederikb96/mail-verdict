/** Focused mail index atom for keyboard navigation. */

import { atom } from "jotai";

/** Index of the focused mail in the current list (keyboard navigation). */
export const focusedMailIndexAtom = atom<number>(-1);
