"use client";

import { AlertCircle, CloudOff } from "lucide-react";
import { useDrainerStatus, useIntentLedger } from "@/hooks/use-intent-ledger";
import { retryIntent } from "@/lib/intent-drainer";
import { retireIntents } from "@/lib/intent-ledger";

/**
 * Mail actions that have not reached the server, said out loud: how many
 * are waiting for the network, and how many the server refused. Absent
 * while everything is going through.
 */
export function ActionsIndicator() {
  const { intents } = useIntentLedger();
  const { waitingForNetwork } = useDrainerStatus();
  const waiting = intents.filter((i) => i.state === "pending" || i.state === "inflight").length;
  const failed = intents.filter((i) => i.state === "failed");

  return (
    <>
      {waitingForNetwork && waiting > 0 && (
        <span
          role="status"
          data-testid="actions-waiting"
          className="flex items-center gap-1.5 rounded-full bg-amber-500/15 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-400"
        >
          <CloudOff className="h-3 w-3" />
          {waiting} action{waiting === 1 ? "" : "s"} waiting for the network
        </span>
      )}
      {failed.length > 0 && (
        <span
          role="status"
          data-testid="actions-failed"
          className="flex items-center gap-1.5 rounded-full bg-destructive/10 px-2 py-0.5 text-xs text-destructive"
        >
          <AlertCircle className="h-3 w-3" />
          {failed.length} action{failed.length === 1 ? "" : "s"} failed
          <button
            type="button"
            className="font-medium underline underline-offset-2"
            onClick={() => failed.forEach((i) => retryIntent(i.id))}
          >
            Retry
          </button>
          <button
            type="button"
            className="font-medium underline underline-offset-2"
            onClick={() => retireIntents(failed.map((i) => i.id))}
          >
            Discard
          </button>
        </span>
      )}
    </>
  );
}
