"use client";

import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

interface DiscardChangesDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDiscard: () => void;
  onSaveDraft: () => void;
  isSaving?: boolean;
}

/**
 * The three-way prompt shown when closing a composer with unsaved work:
 * save it as a draft, discard it, or go back. Shared by the compose
 * dialog, the reply box and the draft editor -- the surface each closes
 * out of differs, but what "unsaved work" means to a composer does not.
 */
export function DiscardChangesDialog({
  open,
  onOpenChange,
  onDiscard,
  onSaveDraft,
  isSaving = false,
}: DiscardChangesDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Save this message?</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          You have unsaved changes. Save it as a draft, or discard it?
        </p>
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={onDiscard}>
            Discard
          </Button>
          <Button disabled={isSaving} onClick={onSaveDraft}>
            {isSaving && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
            Save draft
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
