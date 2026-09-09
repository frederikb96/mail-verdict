"use client";

import { useAtomValue } from "jotai";
import { AlertTriangle } from "lucide-react";
import Link from "next/link";

import { useOutboxList } from "@/hooks/use-outbox";
import { useAccounts } from "@/hooks/use-accounts";
import { selectedAccountIdAtom, isUnifiedViewAtom } from "@/lib/atoms";

/**
 * Persistent banner for the current account's dead outbox items — messages
 * PostIMAP gave up retrying, most commonly because the account has no SMTP
 * configured.
 */
export function OutboxDeadBanner() {
  const accountId = useAtomValue(selectedAccountIdAtom);
  const isUnified = useAtomValue(isUnifiedViewAtom);
  const scopedToOne = !isUnified && !!accountId;
  const { data: dead } = useOutboxList({
    account_id: scopedToOne ? accountId : undefined,
    status: "dead",
  });
  const { data: accounts } = useAccounts();

  if (!dead || dead.length === 0) return null;

  // "This account" is only true when the banner is actually scoped to
  // one -- in the unified view (or with none selected yet) the count
  // can span several, and naming which of them needs fixing is the
  // whole point of showing a message at all here.
  const names = scopedToOne
    ? null
    : Array.from(new Set(dead.map((row) => row.account_id)))
        .map((id) => accounts?.find((a) => a.id === id)?.name)
        .filter((name): name is string => !!name);

  return (
    <div className="flex items-center gap-2 border-b bg-destructive/10 px-3 py-1.5 text-sm text-destructive">
      <AlertTriangle className="h-4 w-4 shrink-0" />
      <span>
        {dead.length} message{dead.length > 1 ? "s" : ""} could not be sent —
        check SMTP settings on {names && names.length > 0 ? names.join(", ") : "this account"}.
      </span>
      <Link href="/accounts" className="ml-auto shrink-0 underline">
        Open account settings
      </Link>
    </div>
  );
}
