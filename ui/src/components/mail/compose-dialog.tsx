"use client";

import { useEffect, useState } from "react";
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
import { ComposeForm } from "@/components/mail/compose-form";
import { useAccounts } from "@/hooks/use-accounts";
import { composeIntentAtom, selectedAccountIdAtom } from "@/lib/atoms";

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
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setComposeIntent(null);
      }}
    >
      <DialogTrigger render={<Button className="w-full justify-start gap-2" />}>
        <PenSquare className="h-4 w-4" />
        Compose
      </DialogTrigger>
      <DialogContent className="max-w-lg">
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
          />
        )}
        {!effectiveAccountId && (
          <p className="text-sm text-muted-foreground">
            Add an account before composing a message.
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}
