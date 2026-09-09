"use client";

/** Shown inside the attachment preview overlay for a type it does not
 * render, one that failed to decode, or one refused for being too large --
 * never a blank overlay, and Download is always the way out. Nothing is
 * fetched to reach this state: an ineligible or oversized file is routed
 * here before any request for its bytes is made. */

import { Download, FileWarning } from "lucide-react";

export function PreviewUnavailable({
  url,
  filename,
  reason,
}: {
  url: string;
  filename: string;
  reason: string;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <FileWarning className="h-10 w-10 text-muted-foreground" />
      <p className="text-sm text-muted-foreground">{reason}</p>
      <a
        href={url}
        download={filename}
        className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
      >
        <Download className="h-4 w-4" />
        Download
      </a>
    </div>
  );
}
