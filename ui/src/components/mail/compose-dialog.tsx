"use client";

import { useEffect, useRef, useState } from "react";
import { useAtom, useAtomValue } from "jotai";
import { PenSquare } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ComposeForm, type ComposeFormControls } from "@/components/mail/compose-form";
import { DiscardChangesDialog } from "@/components/mail/discard-changes-dialog";
import { useAccounts } from "@/hooks/use-accounts";
import { composeIntentAtom, selectedAccountIdAtom } from "@/lib/atoms";
import { cn } from "@/lib/utils";

/**
 * New-mail dialog, reachable from the sidebar and also opened from anywhere
 * else in the app through `composeIntentAtom` (a contact's email, an
 * event's "email the attendees") without owning its own trigger there.
 */
export function ComposeDialog() {
  const { data: accounts } = useAccounts();
  const currentAccountId = useAtomValue(selectedAccountIdAtom);
  const [open, setOpen] = useState(false);
  const [accountId, setAccountId] = useState<string | undefined>(undefined);
  const [composeIntent, setComposeIntent] = useAtom(composeIntentAtom);
  const [isDirty, setIsDirty] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const controlsRef = useRef<ComposeFormControls | null>(null);

  useEffect(() => {
    if (composeIntent) {
      setAccountId(composeIntent.accountId);
      setOpen(true);
    }
  }, [composeIntent]);

  const effectiveAccountId =
    accountId ??
    (currentAccountId && currentAccountId !== "unified" ? currentAccountId : undefined) ??
    accounts?.[0]?.id;

  const closeAndClearIntent = () => {
    setOpen(false);
    setComposeIntent(null);
    setIsDirty(false);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next, eventDetails) => {
        // Escape, an outside click and the dialog's own close button all
        // reach here as the same request -- pausing every one of them on
        // unsaved work is what closes the gap the compose dialog used to
        // have: any of the three discarded a message in progress with no
        // warning at all.
        if (!next && isDirty) {
          eventDetails.cancel();
          setConfirmClose(true);
          return;
        }
        setOpen(next);
        if (!next) setComposeIntent(null);
      }}
    >
      <DialogTrigger render={<Button className="w-full justify-start gap-2" />}>
        <PenSquare className="h-4 w-4" />
        Compose
      </DialogTrigger>
      <DialogContent
        size="lg"
        className={cn(maximized && "flex flex-col")}
        // Inline rather than a class: the maximized size is viewport-relative and has
        // to override the size prop's own max-width, which no utility class can do
        // reliably across breakpoints.
        style={maximized ? { width: "95vw", height: "92vh", maxWidth: "none" } : undefined}
      >
        <DialogHeader>
          <DialogTitle>New Message</DialogTitle>
        </DialogHeader>
        {accounts && accounts.length > 1 && (
          <Select
            value={effectiveAccountId}
            onValueChange={(value) => setAccountId(value ?? undefined)}
          >
            <SelectTrigger className="h-8">
              {/* The underlying control only resolves a label itself when
                  given an item list, which nothing here passes -- without
                  this it renders the account's raw id. */}
              <SelectValue placeholder="From account">
                {(v: string) => accounts?.find((a) => a.id === v)?.name ?? "From account"}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {accounts.map((a) => (
                <SelectItem key={a.id} value={a.id}>
                  {a.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {effectiveAccountId && (
          <ComposeForm
            key={open ? "open" : "closed"}
            accountId={effectiveAccountId}
            defaultTo={composeIntent?.to}
            defaultSubject={composeIntent?.subject}
            onDone={closeAndClearIntent}
            onDirtyChange={setIsDirty}
            onMaximizedChange={setMaximized}
            onControlsReady={(controls) => {
              controlsRef.current = controls;
            }}
          />
        )}
        {!effectiveAccountId && (
          <p className="text-sm text-muted-foreground">
            Add an account before composing a message.
          </p>
        )}
      </DialogContent>
      <DiscardChangesDialog
        open={confirmClose}
        onOpenChange={setConfirmClose}
        onDiscard={() => {
          setConfirmClose(false);
          closeAndClearIntent();
        }}
        onSaveDraft={() => {
          setConfirmClose(false);
          controlsRef.current?.saveDraft();
        }}
      />
    </Dialog>
  );
}
