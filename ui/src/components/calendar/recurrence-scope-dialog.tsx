"use client";

/**
 * Asked before any change to an instance of a recurring series -- including
 * a drag. "This and following" needs a series split the API does not yet
 * support (`scope: "following"`); until it does, this dialog offers only
 * the two scopes that actually work rather than a third that would appear
 * to do the right thing and silently do the wrong one.
 */

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { RecurrenceScope } from "@/types/api";

interface RecurrenceScopeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Present when confirming will mail the event's guests: what they are
   * about to be told, and how many of them. */
  guestNotice?: { action: "cancellation" | "update"; count: number };
  onConfirm: (scope: RecurrenceScope) => void;
}

export function RecurrenceScopeDialog({
  open,
  onOpenChange,
  guestNotice,
  onConfirm,
}: RecurrenceScopeDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Change recurring event</DialogTitle>
        </DialogHeader>
        {guestNotice ? (
          <p className="text-sm text-muted-foreground">
            {guestNotice.action === "cancellation" ? "A cancellation" : "An update"} will be sent to{" "}
            {guestNotice.count} guest{guestNotice.count === 1 ? "" : "s"}.
          </p>
        ) : null}
        <div className="flex flex-col gap-2">
          <Button
            variant="outline"
            className="justify-start"
            autoFocus
            onClick={() => onConfirm("this")}
          >
            This event
          </Button>
          <Button variant="outline" className="justify-start" onClick={() => onConfirm("all")}>
            All events
          </Button>
        </div>
        <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
          Cancel
        </Button>
      </DialogContent>
    </Dialog>
  );
}
