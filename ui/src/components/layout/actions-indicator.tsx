"use client";

import { AlertCircle, CloudOff, History, RefreshCw } from "lucide-react";
import { useDrainerStatus, useIntentLedger } from "@/hooks/use-intent-ledger";
import { discardIntent, retryIntent, sendHeldIntent } from "@/hooks/use-mail-intents";

/**
 * Mail actions that have not reached the server, said out loud: how many
 * are waiting for the network or retrying a failing server, how many the
 * server refused, how many were held for being old, and whether this
 * browser can keep them at all. Absent while everything is going through.
 */
export function ActionsIndicator() {
  const { intents, persistenceFailed } = useIntentLedger();
  const { waitingForNetwork } = useDrainerStatus();
  const unsettled = intents.filter((i) => i.state === "pending" || i.state === "inflight");
  const retrying = unsettled.filter((i) => i.attempts > 0 && i.lastError);
  const failed = intents.filter((i) => i.state === "failed");
  const held = intents.filter((i) => i.state === "held");
  const plural = (n: number) => `${n} action${n === 1 ? "" : "s"}`;
  const oldest = held.reduce((min, i) => Math.min(min, i.createdAt), Number.POSITIVE_INFINITY);

  return (
    <>
      {waitingForNetwork && unsettled.length > 0 ? (
        <Pill testId="actions-waiting" tone="warn" icon={<CloudOff className="h-3 w-3" />}>
          {plural(unsettled.length)} waiting for the network
        </Pill>
      ) : retrying.length > 0 ? (
        <Pill testId="actions-retrying" tone="warn" icon={<RefreshCw className="h-3 w-3" />}>
          {plural(retrying.length)} retrying — the server is not answering
        </Pill>
      ) : null}
      {held.length > 0 && (
        <Pill testId="actions-held" tone="warn" icon={<History className="h-3 w-3" />}>
          {plural(held.length)} from{" "}
          {new Date(oldest).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}{" "}
          never sent
          <Action onClick={() => held.forEach((i) => sendHeldIntent(i.id))}>Send</Action>
          <Action onClick={() => held.forEach((i) => discardIntent(i.id))}>Discard</Action>
        </Pill>
      )}
      {failed.length > 0 && (
        <Pill testId="actions-failed" tone="error" icon={<AlertCircle className="h-3 w-3" />}>
          {plural(failed.length)} failed
          <Action onClick={() => failed.forEach((i) => retryIntent(i.id))}>Retry</Action>
          <Action onClick={() => failed.forEach((i) => discardIntent(i.id))}>Discard</Action>
        </Pill>
      )}
      {persistenceFailed && unsettled.length > 0 && (
        <Pill testId="actions-unsaved" tone="error" icon={<AlertCircle className="h-3 w-3" />}>
          Browser storage is full — actions will be lost on reload
        </Pill>
      )}
    </>
  );
}

function Pill({
  testId, tone, icon, children,
}: {
  testId: string;
  tone: "warn" | "error";
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <span
      role="status"
      data-testid={testId}
      className={
        tone === "warn"
          ? "flex items-center gap-1.5 rounded-full bg-amber-500/15 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-400"
          : "flex items-center gap-1.5 rounded-full bg-destructive/10 px-2 py-0.5 text-xs text-destructive"
      }
    >
      {icon}
      {children}
    </span>
  );
}

function Action({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" className="font-medium underline underline-offset-2" onClick={onClick}>
      {children}
    </button>
  );
}
