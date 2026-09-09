"use client";

/** The image path of the attachment preview overlay: a same-origin `<img>`
 * at the attachment endpoint, exactly the mechanism already proven for
 * every inline `cid:` image in a message body. No fetch, no blob, no
 * client-side decoding step -- the browser does all of it. `touch-action`
 * is left at its default so the browser's own pinch-to-zoom handles the
 * phone case for free; the buttons below are the desktop equivalent. */

import { useState } from "react";
import { ZoomIn, ZoomOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PreviewUnavailable } from "@/components/mail/preview-unavailable";

const MIN_SCALE = 0.25;
const MAX_SCALE = 4;
const SCALE_STEP = 0.25;

export function AttachmentImagePreview({ src, alt }: { src: string; alt: string }) {
  const [scale, setScale] = useState(1);
  const [failed, setFailed] = useState(false);

  if (failed) {
    return <PreviewUnavailable url={src} filename={alt} reason="This file could not be previewed." />;
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-1 min-h-0 items-center justify-center overflow-auto p-4">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={alt}
          onError={() => setFailed(true)}
          style={{ transform: `scale(${scale})` }}
          className="max-h-full max-w-full object-contain transition-transform"
        />
      </div>
      <div className="flex shrink-0 items-center justify-center gap-2 border-t p-2">
        <Button
          variant="outline"
          size="icon-sm"
          onClick={() => setScale((s) => Math.max(MIN_SCALE, s - SCALE_STEP))}
          title="Zoom out"
          aria-label="Zoom out"
        >
          <ZoomOut className="h-3.5 w-3.5" />
        </Button>
        <span className="w-12 text-center text-xs tabular-nums text-muted-foreground">
          {Math.round(scale * 100)}%
        </span>
        <Button
          variant="outline"
          size="icon-sm"
          onClick={() => setScale((s) => Math.min(MAX_SCALE, s + SCALE_STEP))}
          title="Zoom in"
          aria-label="Zoom in"
        >
          <ZoomIn className="h-3.5 w-3.5" />
        </Button>
        <Button variant="outline" size="sm" onClick={() => setScale(1)}>
          Reset
        </Button>
      </div>
    </div>
  );
}
