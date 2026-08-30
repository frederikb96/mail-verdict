/** Jotai atom backing the lightweight toast stack. */

import { atom } from "jotai";

export interface ToastItem {
  id: string;
  message: string;
  variant: "info" | "success" | "warning" | "error";
}

export const toastsAtom = atom<ToastItem[]>([]);
