/** Push/dismiss handles for the toast stack. */

import { useCallback } from "react";
import { useSetAtom } from "jotai";
import { toastsAtom, type ToastItem } from "@/store/toast-atom";

let counter = 0;

export function useToast() {
  const setToasts = useSetAtom(toastsAtom);

  const push = useCallback(
    (
      message: string,
      variant: ToastItem["variant"] = "info",
      durationMs = 5000,
      action?: ToastItem["action"],
    ) => {
      const id = `toast-${Date.now()}-${counter++}`;
      setToasts((prev) => [...prev, { id, message, variant, action }]);
      if (durationMs > 0) {
        setTimeout(() => {
          setToasts((prev) => prev.filter((t) => t.id !== id));
        }, durationMs);
      }
      return id;
    },
    [setToasts],
  );

  const dismiss = useCallback(
    (id: string) => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    },
    [setToasts],
  );

  return { push, dismiss };
}
