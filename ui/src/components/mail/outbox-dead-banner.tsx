"use client";

import { useAtomValue } from "jotai";
import { AlertTriangle } from "lucide-react";
import Link from "next/link";

import { useOutboxList } from "@/hooks/use-outbox";
import { selectedAccountIdAtom, isUnifiedViewAtom } from "@/lib/atoms";

/**
 * Persistent banner for the current account's dead outbox items — messages
 * PostIMAP gave up retrying, most commonly because the account has no SMTP
 * configured.
 */
export function OutboxDeadBanner() {
  const accountId = useAtomValue(selectedAccountIdAtom);
  const isUnified = useAtomValue(isUnifiedViewAtom);
  const { data: dead } = useOutboxList({
    account_id: isUnified || !accountId ? undefined : accountId,
    status: "dead",
  });

  if (!dead || dead.length === 0) return null;

  return (
    <div className="flex items-center gap-2 border-b bg-destructive/10 px-3 py-1.5 text-sm text-destructive">
      <AlertTriangle className="h-4 w-4 shrink-0" />
      <span>
        {dead.length} message{dead.length > 1 ? "s" : ""} could not be sent —
        check SMTP settings on this account.
      </span>
      <Link href="/accounts" className="ml-auto shrink-0 underline">
        Open account settings
      </Link>
    </div>
  );
}
