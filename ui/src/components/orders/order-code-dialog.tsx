"use client";

import { useEffect } from "react";
import { XIcon } from "lucide-react";
import { Dialog, DialogClose, DialogContent, DialogTitle } from "@/components/ui/dialog";
import type { OrderFact } from "@/types/api";

/** Module size of the full-screen drawing -- the largest the server draws,
 * so a phone's whole width still shows crisp modules. */
export const CODE_FULLSCREEN_SCALE = 16;

/** Keeps the screen awake while a code is up, where the browser allows it.
 * Best effort: no wake lock API, or a refusal, just means the screen may
 * dim as usual. */
function useScreenAwake(active: boolean) {
  useEffect(() => {
    if (!active || !("wakeLock" in navigator)) return;
    let cancelled = false;
    let lock: WakeLockSentinel | null = null;
    navigator.wakeLock
      .request("screen")
      .then((sentinel) => {
        if (cancelled) void sentinel.release();
        else lock = sentinel;
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      void lock?.release();
    };
  }, [active]);
}

/** A ticket's code at the size a scanner wants: the whole screen, black on
 * white whatever the theme, closed by a tap anywhere. */
export function OrderCodeDialog({
  fact,
  onOpenChange,
}: {
  fact: OrderFact | null;
  onOpenChange: (open: boolean) => void;
}) {
  const code = fact?.code ?? null;
  useScreenAwake(code !== null);
  if (!fact || !code) return null;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent
        size="full"
        showCloseButton={false}
        overlayBlur={false}
        data-testid="order-code-dialog"
        className="flex flex-col items-center justify-center gap-4 bg-white p-6 text-neutral-900 ring-0"
        onClick={() => onOpenChange(false)}
      >
        <DialogClose
          aria-label="Close"
          className="absolute right-3 top-3 inline-flex h-10 w-10 items-center justify-center rounded-full text-neutral-700 hover:bg-neutral-100"
        >
          <XIcon className="h-5 w-5" />
        </DialogClose>
        {/* eslint-disable-next-line @next/next/no-img-element -- a generated PNG, scaled by the browser */}
        <img
          src={`${code.image_url}?scale=${CODE_FULLSCREEN_SCALE}`}
          alt={`${fact.label} (${code.format})`}
          draggable={false}
          style={{
            width: "calc(100dvw - 48px)",
            height: "calc(100dvh - 140px)",
            objectFit: "contain",
            imageRendering: "pixelated",
          }}
          className="bg-white"
        />
        <div className="max-w-full text-center">
          <DialogTitle className="text-base font-semibold text-neutral-900">{fact.label}</DialogTitle>
          {code.payload_text && (
            <p className="mt-1 break-all font-mono text-sm text-neutral-600">{code.payload_text}</p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
