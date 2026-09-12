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
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ComposeForm, type ComposeFormControls } from "@/components/mail/compose-form";
import { DiscardChangesDialog } from "@/components/mail/discard-changes-dialog";
import { useAccounts } from "@/hooks/use-accounts";
import { useIdentities } from "@/hooks/use-identities";
import { composeIntentAtom, selectedAccountIdAtom } from "@/lib/atoms";
import { cn } from "@/lib/utils";
import type { AccountResponse, Identity } from "@/types/api";

/** One selectable "From" address, grouped per account -- either a real
 * identity row, or, for an account that has never had one added, a stand-in
 * for the address it would actually send with (accounts.imap_user, the
 * same fallback resolve_send_from_addr uses server-side). Without the
 * stand-in, an account with no identity of its own would offer no way at
 * all to pick which account a fresh message sends from -- the one thing
 * the account picker this replaced did unconditionally. `identityId` is
 * only set for a real row: the stand-in must never be sent as
 * identity_id, since it names nothing that exists.
 */
interface PickableAddress {
  key: string;
  accountId: string;
  identityId: string | undefined;
  address: string;
  displayName: string | null;
  isDefault: boolean;
}

/** "Frederik Berg <frederik.berg@posteo.net>", or the bare address when
 * there's no display name -- what actually distinguishes several
 * identities sharing one display name is the address, so it is never
 * dropped from the label. */
function formatAddressLabel(entry: Pick<PickableAddress, "displayName" | "address">): string {
  return entry.displayName ? `${entry.displayName} <${entry.address}>` : entry.address;
}

function pickableAddresses(
  accounts: AccountResponse[], identities: Identity[],
): PickableAddress[] {
  return accounts.flatMap((account): PickableAddress[] => {
    const real = identities.filter((i) => i.account_id === account.id);
    if (real.length > 0) {
      return real.map((identity) => ({
        key: identity.id,
        accountId: account.id,
        identityId: identity.id,
        address: identity.address,
        displayName: identity.display_name,
        isDefault: identity.is_default,
      }));
    }
    return [
      {
        key: `account-default:${account.id}`,
        accountId: account.id,
        identityId: undefined,
        address: account.imap_user,
        displayName: null,
        isDefault: true,
      },
    ];
  });
}

function defaultAddressForAccount(
  addresses: PickableAddress[], accountId: string | undefined,
): PickableAddress | undefined {
  if (!accountId) return undefined;
  const forAccount = addresses.filter((a) => a.accountId === accountId);
  return forAccount.find((a) => a.isDefault) ?? forAccount[0];
}

/**
 * New-mail dialog, reachable from the sidebar and also opened from anywhere
 * else in the app through `composeIntentAtom` (a contact's email, an
 * event's "email the attendees", an undone send's own reopen) without
 * owning its own trigger there.
 */
export function ComposeDialog() {
  const { data: accounts } = useAccounts();
  const { data: identities } = useIdentities();
  const currentAccountId = useAtomValue(selectedAccountIdAtom);
  const [open, setOpen] = useState(false);
  const [pickedKey, setPickedKey] = useState<string | undefined>(undefined);
  const [composeIntent, setComposeIntent] = useAtom(composeIntentAtom);
  const [isDirty, setIsDirty] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const controlsRef = useRef<ComposeFormControls | null>(null);

  const addresses = pickableAddresses(accounts ?? [], identities ?? []);

  useEffect(() => {
    if (composeIntent) {
      setPickedKey(composeIntent.identityId);
      setOpen(true);
    }
  }, [composeIntent]);

  // One From control spanning every account -- which account this message
  // sends from follows from which address is picked, rather than the
  // other way around (the account picker this replaced chose the account
  // first and left the address to ComposeForm's own, separate select).
  const effectiveAddress =
    addresses.find((a) => a.key === pickedKey) ??
    defaultAddressForAccount(addresses, composeIntent?.accountId) ??
    defaultAddressForAccount(
      addresses,
      currentAccountId && currentAccountId !== "unified" ? currentAccountId : undefined,
    ) ??
    defaultAddressForAccount(addresses, accounts?.[0]?.id) ??
    addresses[0];
  const effectiveAccountId = effectiveAddress?.accountId ?? accounts?.[0]?.id;

  const closeAndClearIntent = () => {
    setOpen(false);
    // A close asked for while a send was in flight raised the unsaved-work
    // prompt; the send completing is what closes the dialog now, and the
    // prompt goes with it rather than staying over the page with nothing
    // left to save.
    setConfirmClose(false);
    setComposeIntent(null);
    setIsDirty(false);
    // A submit already clears its own recovery buffer -- this is what
    // makes an explicit discard clear the one case it does not cover.
    controlsRef.current?.clearRecovery();
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
        {addresses.length > 1 && (
          <div className="grid grid-cols-[auto_1fr] items-center gap-2">
            <span className="text-xs text-muted-foreground">From</span>
            <Select
              value={effectiveAddress?.key}
              onValueChange={(value) => setPickedKey(value ?? undefined)}
            >
              <SelectTrigger className="h-8">
                <SelectValue placeholder="From address">
                  {(v: string) => {
                    const found = addresses.find((a) => a.key === v);
                    return found ? formatAddressLabel(found) : "From address";
                  }}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {(accounts ?? []).map((account) => {
                  const accountAddresses = addresses.filter((a) => a.accountId === account.id);
                  if (accountAddresses.length === 0) return null;
                  return (
                    <SelectGroup key={account.id}>
                      {accounts && accounts.length > 1 && (
                        <SelectLabel>{account.name}</SelectLabel>
                      )}
                      {accountAddresses.map((entry) => (
                        <SelectItem key={entry.key} value={entry.key}>
                          {formatAddressLabel(entry)}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  );
                })}
              </SelectContent>
            </Select>
          </div>
        )}
        {effectiveAccountId && (
          <ComposeForm
            key={open ? "open" : "closed"}
            accountId={effectiveAccountId}
            defaultIdentityId={effectiveAddress?.identityId}
            hideIdentityPicker
            defaultTo={composeIntent?.to}
            defaultCc={composeIntent?.cc}
            defaultBcc={composeIntent?.bcc}
            defaultSubject={composeIntent?.subject}
            defaultBodyHtml={composeIntent?.bodyHtml}
            inReplyTo={composeIntent?.inReplyTo}
            references={composeIntent?.references}
            replacesMessageId={composeIntent?.replacesMessageId}
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
