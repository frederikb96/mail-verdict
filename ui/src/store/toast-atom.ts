/** Jotai atom backing the lightweight toast stack. */

import { atom } from "jotai";

export interface ToastItem {
  id: string;
  message: string;
  variant: "info" | "success" | "warning" | "error";
  /** An optional inline action, e.g. "Undo" on a calendar move/resize. */
  action?: { label: string; onClick: () => void };
}

export const toastsAtom = atom<ToastItem[]>([]);
