"use client";

import { useAtomValue } from "jotai";
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
import { selectedAccountIdAtom } from "@/lib/atoms";
import { useState } from "react";

/** New-mail dialog, reachable from the sidebar. */
export function ComposeDialog() {
  const { data: accounts } = useAccounts();
  const currentAccountId = useAtomValue(selectedAccountIdAtom);
  const [open, setOpen] = useState(false);
  const [accountId, setAccountId] = useState<string | undefined>(undefined);

  const effectiveAccountId =
    accountId ??
    (currentAccountId && currentAccountId !== "unified" ? currentAccountId : undefined) ??
    accounts?.[0]?.id;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
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
              <SelectValue placeholder="From account" />
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
            accountId={effectiveAccountId}
            onDone={() => setOpen(false)}
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
