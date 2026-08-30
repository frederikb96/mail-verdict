"use client";

import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";

import { formatFullDate } from "@/lib/format";
import { usePipelineRevisions, useRestorePipelineRevision } from "@/hooks/use-pipeline";

export function RevisionsDialog({
  open,
  onOpenChange,
  currentRevision,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentRevision: number;
}) {
  const { data: revisions, isLoading } = usePipelineRevisions();
  const restore = useRestorePipelineRevision();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh] max-w-md overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Revision history</DialogTitle>
        </DialogHeader>
        {isLoading && <Skeleton className="h-32 w-full" />}
        <div className="flex flex-col gap-2">
          {revisions?.map((rev) => (
            <div
              key={rev.revision}
              className="flex items-center justify-between rounded-md border p-2 text-sm"
            >
              <div>
                <div className="font-medium">
                  Revision {rev.revision}
                  {rev.revision === currentRevision && (
                    <span className="ml-2 text-xs text-muted-foreground">current</span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground">
                  {formatFullDate(rev.created_at)}
                  {rev.note ? ` — ${rev.note}` : ""}
                </div>
              </div>
              {rev.revision !== currentRevision && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={restore.isPending}
                  onClick={() =>
                    restore.mutate(rev.revision, { onSuccess: () => onOpenChange(false) })
                  }
                >
                  {restore.isPending ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    "Restore"
                  )}
                </Button>
              )}
            </div>
          ))}
          {revisions && revisions.length === 0 && (
            <p className="text-sm text-muted-foreground">No revisions yet.</p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
