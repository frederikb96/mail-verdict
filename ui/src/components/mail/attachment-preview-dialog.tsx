"use client";

/** Full-screen preview for an attachment on a received message -- opened
 * from the attachment chip in ThreadMessage, next to the download control
 * it does not replace. Decides eligibility from the attachment's own
 * content_type, never fetches anything for a type it does not render, and
 * falls back to "no preview" (with Download still offered) for everything
 * it does not recognise or that is too large to attempt. */

import { Download } from "lucide-react";
import { AttachmentImagePreview } from "@/components/mail/attachment-image-preview";
import { AttachmentPdfPreview } from "@/components/mail/attachment-pdf-preview";
import { PreviewUnavailable } from "@/components/mail/preview-unavailable";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { formatSize } from "@/lib/format";
import type { AttachmentSummary } from "@/types/api";

/** UX safety valve, not a security control -- above this, a preview is
 * refused rather than risking hanging the tab on a huge multi-hundred-page
 * PDF or a multi-hundred-megabyte image. Comparable in spirit to
 * ROOT_COLOR_SCAN_DEPTH in email-renderer.tsx: a frontend-only constant,
 * not a config.yaml entry, since it governs no user-configurable
 * behaviour. */
const MAX_PREVIEW_BYTES = 25 * 1024 * 1024;

type PreviewKind = "image" | "pdf" | "none";

function previewKind(contentType: string | null): PreviewKind {
  if (!contentType) return "none";
  if (contentType.startsWith("image/")) return "image";
  if (contentType === "application/pdf") return "pdf";
  return "none";
}

export function AttachmentPreviewDialog({
  messageId,
  attachment,
  onOpenChange,
}: {
  messageId: string;
  attachment: AttachmentSummary;
  onOpenChange: (open: boolean) => void;
}) {
  const url = api.mails.attachmentUrl(messageId, attachment.id);
  const filename = attachment.filename ?? "Attachment";
  const kind = previewKind(attachment.content_type);
  const tooLarge =
    attachment.size_bytes !== null && attachment.size_bytes > MAX_PREVIEW_BYTES;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent size="full" className="flex flex-col gap-0 p-0">
        <DialogHeader className="flex-row items-center justify-between gap-3 border-b p-3 pr-12">
          <div className="flex min-w-0 flex-col">
            <DialogTitle className="truncate">{filename}</DialogTitle>
            {attachment.size_bytes !== null && (
              <span className="text-xs text-muted-foreground">
                {formatSize(attachment.size_bytes)}
              </span>
            )}
          </div>
          <a
            href={url}
            download={filename}
            className="flex shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
            title={`Download ${filename}`}
            aria-label={`Download ${filename}`}
          >
            <Download className="h-3.5 w-3.5" />
            Download
          </a>
        </DialogHeader>

        <div className="min-h-0 flex-1">
          {kind === "none" || tooLarge ? (
            <PreviewUnavailable
              url={url}
              filename={filename}
              reason={tooLarge ? "This file is too large to preview." : "No preview available for this file."}
            />
          ) : kind === "image" ? (
            <AttachmentImagePreview key={attachment.id} src={url} alt={filename} />
          ) : (
            <AttachmentPdfPreview key={attachment.id} src={url} />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
