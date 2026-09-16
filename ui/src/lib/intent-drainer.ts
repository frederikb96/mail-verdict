/**
 * Sends the ledger's intents to the server.
 *
 * One request out per account at a time, earliest intent first, and never
 * one overtaking an earlier unsettled intent on the same message
 * (sendableIntents). Each request carries the intent's id as its
 * idempotency key and gives up after REQUEST_TIMEOUT_MS, so a stalled
 * connection is a retry rather than a request hanging for minutes -- and a
 * retry of a request whose response was lost is answered by the server
 * without acting twice.
 *
 * A network error or timeout holds every intent back until the network
 * answers again (with backoff, and at once when the browser reports being
 * back online); a 408, 429 or 5xx retries that intent with backoff; a 404
 * retires it without a word, the message being gone; any other refusal
 * marks it failed for the person to retry or dismiss.
 *
 * Only one tab sends: the one holding a Web Lock. Every other tab still
 * projects the same intents, and takes over when that tab closes.
 */

import { onlineManager } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import {
  addIntents,
  dropUndoFor,
  getLedgerSnapshot,
  retireIntents,
  subscribeLedger,
  sweepLedger,
  updateIntent,
} from "@/lib/intent-ledger";
import { newIdempotencyKey } from "@/lib/idempotency-key";
import {
  classifyFailure,
  nextWakeAt,
  retryDelay,
  reversalsOf,
  sendableIntents,
  type MailIntent,
} from "@/lib/mail-intents";

const REQUEST_TIMEOUT_MS = 15_000;
const SWEEP_INTERVAL_MS = 30_000;
const LOCK_NAME = "mail-verdict-intent-drainer";

export interface DrainerHooks {
  /** A request succeeded -- the views it touched need re-reading. */
  onSettled(intent: MailIntent): void;
  /** The server refused an intent. */
  onFailed(intent: MailIntent): void;
  /** A reversal found nothing left to move back. */
  onNothingToUndo(intent: MailIntent): void;
}

export interface DrainerStatus {
  /** Intents are waiting because the network is not answering. */
  waitingForNetwork: boolean;
}

let hooks: DrainerHooks | null = null;
let leader = false;
let started = false;
let networkBlockedUntil = 0;
let networkFailures = 0;
let wakeTimer: ReturnType<typeof setTimeout> | null = null;
let status: DrainerStatus = { waitingForNetwork: false };
const statusListeners = new Set<() => void>();
const inflight = new Set<string>();

function setStatus(next: DrainerStatus): void {
  if (next.waitingForNetwork === status.waitingForNetwork) return;
  status = next;
  for (const listener of statusListeners) listener();
}

export function subscribeDrainerStatus(listener: () => void): () => void {
  statusListeners.add(listener);
  return () => statusListeners.delete(listener);
}

export function getDrainerStatus(): DrainerStatus {
  return status;
}

const IDLE_STATUS: DrainerStatus = { waitingForNetwork: false };
export function getServerDrainerStatus(): DrainerStatus {
  return IDLE_STATUS;
}

/** Start sending, once per page. Later calls only replace the hooks. */
export function startDrainer(next: DrainerHooks): void {
  hooks = next;
  if (started || typeof window === "undefined") return;
  started = true;

  onlineManager.subscribe((online) => {
    if (!online) return;
    networkBlockedUntil = 0;
    kickDrainer();
  });
  window.setInterval(() => sweepLedger(), SWEEP_INTERVAL_MS);
  // Another tab adding an intent reaches this one only through the ledger.
  let kickQueued = false;
  subscribeLedger(() => {
    if (kickQueued) return;
    kickQueued = true;
    queueMicrotask(() => {
      kickQueued = false;
      kickDrainer();
    });
  });

  const becomeLeader = () => {
    leader = true;
    // Whatever another tab had out when it closed never came back; the
    // idempotency key makes sending it again safe.
    for (const intent of getLedgerSnapshot().intents) {
      if (intent.state === "inflight") updateIntent(intent.id, { state: "pending" });
    }
    kickDrainer();
  };
  if (typeof navigator !== "undefined" && navigator.locks?.request) {
    void navigator.locks.request(LOCK_NAME, () => {
      becomeLeader();
      return new Promise<void>(() => {});
    });
  } else {
    becomeLeader();
  }
}

/** Look for something to send now. Safe to call as often as anything changes. */
export function kickDrainer(): void {
  if (!leader) return;
  if (wakeTimer) {
    clearTimeout(wakeTimer);
    wakeTimer = null;
  }
  const now = Date.now();
  const { intents } = getLedgerSnapshot();
  const waiting = intents.some((i) => i.state === "pending" || i.state === "inflight");
  const online = onlineManager.isOnline();
  setStatus({ waitingForNetwork: waiting && (!online || networkFailures > 0) });
  if (!online) return;
  if (now < networkBlockedUntil) {
    schedule(networkBlockedUntil - now);
    return;
  }
  for (const intent of sendableIntents(intents, now)) {
    if (inflight.has(intent.id)) continue;
    void send(intent);
  }
  const wake = nextWakeAt(getLedgerSnapshot().intents, now);
  if (wake !== null) schedule(wake - now);
}

function schedule(delay: number): void {
  if (wakeTimer) clearTimeout(wakeTimer);
  wakeTimer = setTimeout(() => {
    wakeTimer = null;
    kickDrainer();
  }, Math.max(0, delay));
}

type Outcome =
  | { ok: true; affected: number; sources?: Array<{ id: string; folderId: string }> }
  | { ok: false; error: string };

async function request(intent: MailIntent): Promise<Outcome> {
  const options = { timeoutMs: REQUEST_TIMEOUT_MS };
  if (!intent.bulk) {
    const response = await api.mails.action(
      intent.messages[0].id,
      { action: intent.action, target_folder_id: intent.targetFolderId, idempotency_key: intent.id },
      options,
    );
    return response.success
      ? { ok: true, affected: 1 }
      : { ok: false, error: response.message ?? `Could not ${intent.action}` };
  }
  const response = await api.messages.bulkAction(
    intent.accountId,
    {
      action: intent.action,
      target_folder_id: intent.targetFolderId,
      ids: intent.messages.map((m) => m.id),
      expand_threads: intent.expandThreads || undefined,
      idempotency_key: intent.id,
    },
    options,
  );
  if (!response.success) {
    return { ok: false, error: response.errors.join("; ") || `Could not ${intent.action}` };
  }
  return {
    ok: true,
    affected: response.affected_count,
    sources: response.sources?.map((s) => ({ id: s.id, folderId: s.folder_id })),
  };
}

async function send(intent: MailIntent): Promise<void> {
  inflight.add(intent.id);
  updateIntent(intent.id, { state: "inflight", attempts: intent.attempts + 1 });
  let outcome: Outcome | null = null;
  try {
    outcome = await request(intent);
  } catch (err) {
    const kind = classifyFailure(err instanceof ApiError ? err.status : null);
    const current = currentIntent(intent.id);
    if (!current) {
      // Retired meanwhile -- nothing to update.
    } else if (kind === "gone") {
      dropUndoFor(intent.id);
      retireIntents([intent.id]);
    } else if (kind === "terminal") {
      settleFailed(current, err instanceof Error ? err.message : String(err));
    } else {
      const now = Date.now();
      const delay = retryDelay(current.attempts, kind);
      updateIntent(intent.id, {
        state: "pending", notBefore: now + delay,
        lastError: err instanceof Error ? err.message : String(err),
      });
      if (kind === "network") {
        networkFailures += 1;
        networkBlockedUntil = now + retryDelay(networkFailures, "network");
      }
    }
  } finally {
    inflight.delete(intent.id);
  }

  if (outcome) {
    networkFailures = 0;
    networkBlockedUntil = 0;
    setStatus({ waitingForNetwork: false });
    const current = currentIntent(intent.id);
    if (current && outcome.ok) settleDone(current, outcome);
    else if (current && !outcome.ok) settleFailed(current, outcome.error);
  }
  kickDrainer();
}

function currentIntent(id: string): MailIntent | undefined {
  return getLedgerSnapshot().intents.find((i) => i.id === id);
}

function settleDone(
  intent: MailIntent,
  outcome: Extract<Outcome, { ok: true }>,
): void {
  const done = updateIntent(intent.id, {
    state: "done", doneAt: Date.now(), lastError: undefined,
    sources: outcome.sources?.length ? outcome.sources : undefined,
  });
  if (!done) return;
  if (done.undoRequested) {
    retireIntents([done.id]);
    addIntents(reversalsOf(done, Date.now(), newIdempotencyKey));
  }
  if (done.reverses !== undefined && outcome.affected === 0) hooks?.onNothingToUndo(done);
  hooks?.onSettled(done);
}

function settleFailed(intent: MailIntent, error: string): void {
  dropUndoFor(intent.id);
  if (intent.undoRequested) {
    retireIntents([intent.id]);
    return;
  }
  const failed = updateIntent(intent.id, { state: "failed", lastError: error });
  if (failed) hooks?.onFailed(failed);
  hooks?.onSettled(intent);
}

/** The server answered on another channel (the event stream reconnected):
 * stop waiting out backoff and try everything waiting now. */
export function networkRecovered(): void {
  networkBlockedUntil = 0;
  const now = Date.now();
  for (const intent of getLedgerSnapshot().intents) {
    if (intent.state === "pending" && intent.notBefore > now) {
      updateIntent(intent.id, { notBefore: now });
    }
  }
  kickDrainer();
}

/** Send a failed intent again, from the start. */
export function retryIntent(id: string): void {
  updateIntent(id, { state: "pending", attempts: 0, notBefore: Date.now(), lastError: undefined });
  kickDrainer();
}
