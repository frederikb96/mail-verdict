"use client";

import { FileWarning } from "lucide-react";

/** Shown instead of a body when the server never fetched it (over the size limit). */
export function TruncatedBanner() {
  return (
    <div className="flex items-center gap-2 border-b bg-muted/50 px-4 py-3 text-sm text-muted-foreground">
      <FileWarning className="h-4 w-4 shrink-0" />
      <span>
        This message is too large to display. Its content was not downloaded
        during sync.
      </span>
    </div>
  );
}
