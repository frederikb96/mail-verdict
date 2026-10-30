"use client";

import { useRef, useState } from "react";
import { Loader2, Paperclip } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useUploadOrderFile } from "@/hooks/use-orders";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api";
import { ACCEPTED_UPLOADS, toUploadable } from "@/lib/order-upload";
import { cn } from "@/lib/utils";

/** "Add file": opens the picker and sends each chosen file through the same
 * pipeline as mail attachments. Given an `orderId` the file joins that
 * entry; without one the pipeline picks (or opens) the entry itself. */
export function OrderFileUploadButton({
  orderId,
  className,
}: {
  orderId?: string;
  className?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const upload = useUploadOrderFile();
  const { push: pushToast } = useToast();
  const [busy, setBusy] = useState(false);
  // Set and read synchronously: a state flag is a render snapshot and two
  // quick picks would both pass it.
  const busyRef = useRef(false);

  const handleFiles = async (files: File[]) => {
    if (busyRef.current || files.length === 0) return;
    busyRef.current = true;
    setBusy(true);
    try {
      for (const picked of files) {
        try {
          const file = await toUploadable(picked);
          const status = await upload.mutateAsync({ file, orderId });
          if (status.status === "attached") {
            pushToast(`Added ${file.name}`, "success", 3000);
          } else if (status.status === "failed") {
            pushToast(`Could not add ${file.name}: ${status.last_error ?? "unreadable file"}`, "error");
          } else {
            pushToast(`${file.name} is still being analysed -- it appears when ready`, "info");
          }
        } catch (error) {
          const reason =
            error instanceof ApiError || error instanceof Error ? error.message : "upload failed";
          pushToast(reason, "error", 8000);
        }
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPTED_UPLOADS}
        className="hidden"
        data-testid="order-file-input"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = "";
          void handleFiles(files);
        }}
      />
      <Button
        variant="outline"
        size="sm"
        disabled={busy}
        className={cn("gap-1.5", className)}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Paperclip className="h-3.5 w-3.5" />}
        Add file
      </Button>
    </>
  );
}
