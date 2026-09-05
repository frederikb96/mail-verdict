"use client";

import { useAtomValue } from "jotai";
import { X } from "lucide-react";
import { toastsAtom, type ToastItem } from "@/store/toast-atom";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";

const VARIANT_STYLES: Record<ToastItem["variant"], string> = {
  info: "bg-background border",
  success: "bg-green-600 text-white border-green-600",
  warning: "bg-amber-500 text-white border-amber-500",
  error: "bg-destructive text-destructive-foreground border-destructive",
};

/** Fixed-position stack of transient status toasts (outbox send/fail/etc). */
export function ToastContainer() {
  const toasts = useAtomValue(toastsAtom);
  const { dismiss } = useToast();

  if (toasts.length === 0) return null;

  return (
    <div
      data-slot="toast-container"
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed bottom-4 right-4 z-[100] flex flex-col gap-2"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          data-slot="toast"
          className={cn(
            "pointer-events-auto flex items-center gap-3 rounded-md border px-3 py-2 text-sm shadow-lg",
            VARIANT_STYLES[toast.variant],
          )}
        >
          <span className="max-w-80">{toast.message}</span>
          {toast.action && (
            <button
              onClick={() => {
                toast.action!.onClick();
                dismiss(toast.id);
              }}
              className="shrink-0 font-medium underline underline-offset-2"
            >
              {toast.action.label}
            </button>
          )}
          <button
            onClick={() => dismiss(toast.id)}
            className="opacity-70 hover:opacity-100"
            aria-label="Dismiss"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}
