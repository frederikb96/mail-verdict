/**
 * Sends the ledger's intents to the server, and carries out undo requests.
 *
 * One request out per account at a time, earliest intent first, and never
 * one overtaking an earlier unsettled intent on the same message
 * (sendableIntents). Each request carries the intent's id as its
 * idempotency key, is guarded to the folder each message was seen in
 * (requestGuards), and gives up after REQUEST_TIMEOUT_MS.
 *
 * - A network error or timeout holds every intent back until the network
 *   answers again: backoff, and at once when the browser reports being
 *   online or the event stream reconnects.
 * - A 408, 425, 429 or 5xx retries that intent with backoff, up to
 *   MAX_RETRY_ATTEMPTS, then marks it failed so the person sees it.
 * - A 404 ends the intent without a word: the message is gone.
 * - Any other refusal marks it failed, for Retry or Discard.
 * - An intent still unsent after PENDING_TTL_MS is held until the person
 *   confirms it -- a guard sees where a message is, not everything that
 *   happened to the mailbox in an hour.
 *
 * Only one tab sends: the one holding a Web Lock, or where Web Locks do not
 * exist (a plain-HTTP deployment) the one holding a short lease in local
 * storage. Every tab projects the same intents, and each tells its own
 * person about the outcome of what they did (use-mail-intents.ts).
 */

import { onlineManager } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import {
  addIntents,
  consumeUndoRequest,
  currentTabId,
  dropUndoFor,
  getLedgerSnapshot,
  restartIntent,
  retireIntents,
  subscribeLedger,
  sweepLedger,
  updateIntent,
} from "@/lib/intent-ledger";
import { newIdempotencyKey } from "@/lib/idempotency-key";
import {
  MAX_RETRY_ATTEMPTS,
  classifyFailure,
  mayHaveLanded,
  nextWakeAt,
  requestGuards,
  retryDelay,
  reversalsOf,
  sendableIntents,
  staleIntents,
  type MailIntent,
  type UndoRequest,
} from "@/lib/mail-intents";

const REQUEST_TIMEOUT_MS = 15_000;
const SWEEP_INTERVAL_MS = 30_000;
const LOCK_NAME = "mail-verdict-intent-drainer";
const LEASE_KEY = "mail-verdict-intent-drainer-lease";
const LEASE_MS = 6_000;
const LEASE_RENEW_MS = 2_000;

export interface DrainerStatus {
  /** Intents are waiting because the network is not answering. */
  waitingForNetwork: boolean;
}

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

/** Start the drainer, once per page. */
export function startDrainer(): void {
  if (started || typeof window === "undefined") return;
  started = true;

  onlineManager.subscribe((online) => {
    if (online) networkRecovered();
  });
  window.setInterval(() => sweepLedger(), SWEEP_INTERVAL_MS);
  // Another tab adding an intent or asking for an undo reaches this one
  // only through the ledger.
  let kickQueued = false;
  subscribeLedger(() => {
    if (kickQueued) return;
    kickQueued = true;
    queueMicrotask(() => {
      kickQueued = false;
      kickDrainer();
    });
  });

  if (typeof navigator !== "undefined" && navigator.locks?.request) {
    void navigator.locks.request(LOCK_NAME, () => {
      becomeLeader();
      return new Promise<void>(() => {});
    });
  } else {
    holdLease();
    window.setInterval(holdLease, LEASE_RENEW_MS);
  }
}

function becomeLeader(): void {
  if (leader) return;
  leader = true;
  // Whatever another tab had out when it closed never came back; the
  // idempotency key makes sending it again safe.
  for (const intent of getLedgerSnapshot().intents) {
    if (intent.state === "inflight") updateIntent(intent.id, { state: "pending" });
  }
  kickDrainer();
}

/** Without Web Locks: take or renew the lease when it is free or ours. */
function holdLease(): void {
  const me = currentTabId();
  const now = Date.now();
  try {
    const lease = JSON.parse(window.localStorage.getItem(LEASE_KEY) ?? "null") as
      | { tab: string; until: number }
      | null;
    if (!lease || lease.until < now || lease.tab === me) {
      window.localStorage.setItem(LEASE_KEY, JSON.stringify({ tab: me, until: now + LEASE_MS }));
    }
    const held = JSON.parse(window.localStorage.getItem(LEASE_KEY) ?? "null") as { tab: string } | null;
    if (held?.tab === me) becomeLeader();
    else leader = false;
  } catch {
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
  for (const stale of staleIntents(intents, now)) {
    if (!inflight.has(stale.id)) updateIntent(stale.id, { state: "held" });
  }
  processUndoRequests();

  const current = getLedgerSnapshot().intents;
  const waiting = current.some((i) => i.state === "pending" || i.state === "inflight");
  const online = onlineManager.isOnline();
  setStatus({ waitingForNetwork: waiting && (!online || networkFailures > 0) });
  if (!online) return;
  if (now < networkBlockedUntil) {
    schedule(networkBlockedUntil - now);
    return;
  }
  for (const intent of sendableIntents(current, now)) {
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

/**
 * Carry out every undo request whose intents have all been answered: an
 * intent never sent or refused is dropped; one applied is reversed from the
 * undo step's copy; one still out is waited for. One that may have landed
 * unanswered is sent again first -- held or given up on, nothing else would
 * ever send it.
 */
function processUndoRequests(): void {
  const { undoRequests } = getLedgerSnapshot();
  for (const request of undoRequests) {
    for (const copy of request.entry.intents) {
      const live = getLedgerSnapshot().intents.find((i) => i.id === copy.id);
      if (live && mayHaveLanded(live) && (live.state === "held" || live.state === "failed")) {
        restartIntent(live.id, { resend: true });
      }
    }
    if (!undoRequestReady(request)) continue;
    const { intents } = getLedgerSnapshot();
    const now = Date.now();
    const retire: string[] = [];
    const reversals: MailIntent[] = [];
    for (const copy of request.entry.intents) {
      const live = intents.find((i) => i.id === copy.id);
      if (live && live.state !== "done") {
        retire.push(live.id);
        continue;
      }
      if (live) retire.push(live.id);
      const merged: MailIntent = live
        ? { ...copy, ...live, messages: copy.messages, originTab: request.originTab }
        : { ...copy, originTab: request.originTab };
      reversals.push(...reversalsOf(merged, now, newIdempotencyKey));
    }
    if (reversals.length > 0) addIntents(reversals, undefined, { keepRows: true });
    retireIntents(retire);
    consumeUndoRequest(request.id);
  }
}

function undoRequestReady(request: UndoRequest): boolean {
  const { intents } = getLedgerSnapshot();
  return request.entry.intents.every((copy) => {
    const live = intents.find((i) => i.id === copy.id);
    if (!live) return true;
    return live.state !== "inflight" && !mayHaveLanded(live);
  });
}

type Outcome = {
  applied: boolean;
  affected: number;
  landedFolderId?: string;
  skippedIds: string[];
  sources?: Array<{ id: string; folderId: string }>;
} | { refused: string };

async function request(intent: MailIntent): Promise<Outcome> {
  const options = { timeoutMs: REQUEST_TIMEOUT_MS };
  const guards = requestGuards(intent);
  if (!intent.bulk) {
    const message = intent.messages[0];
    const response = await api.mails.action(
      message.id,
      {
        action: intent.action, target_folder_id: intent.targetFolderId, idempotency_key: intent.id,
        expected_folder_id: guards.expectedFolderIds[message.id],
      },
      options,
    );
    if (!response.success) return { refused: response.message ?? `Could not ${intent.action}` };
    const applied = response.applied !== false;
    return {
      applied, affected: applied ? 1 : 0, skippedIds: applied ? [] : [message.id],
      landedFolderId: response.folder_id ?? undefined,
    };
  }
  const response = await api.messages.bulkAction(
    intent.accountId,
    {
      action: intent.action,
      target_folder_id: intent.targetFolderId,
      ids: intent.messages.map((m) => m.id),
      expand_threads: intent.expandThreads || undefined,
      expand_threads_through: guards.expandThreadsThrough ?? undefined,
      expected_folder_ids: guards.expectedFolderIds,
      idempotency_key: intent.id,
    },
    options,
  );
  if (!response.success) {
    return { refused: response.errors.join("; ") || `Could not ${intent.action}` };
  }
  const skippedIds = response.skipped_ids ?? [];
  return {
    applied: skippedIds.length < intent.messages.length,
    affected: response.affected_count,
    skippedIds,
    landedFolderId: response.target_folder_id ?? undefined,
    sources: response.sources?.map((s) => ({ id: s.id, folderId: s.folder_id })),
  };
}

async function send(intent: MailIntent): Promise<void> {
  inflight.add(intent.id);
  const now = Date.now();
  updateIntent(intent.id, {
    state: "inflight", attempts: intent.attempts + 1, firstSentAt: intent.firstSentAt ?? now,
  });
  let outcome: Outcome | null = null;
  try {
    outcome = await request(intent);
  } catch (err) {
    const current = currentIntent(intent.id, intent.generation);
    if (current) settleError(current, err);
  } finally {
    inflight.delete(intent.id);
  }

  if (outcome) {
    networkFailures = 0;
    networkBlockedUntil = 0;
    setStatus({ waitingForNetwork: false });
    const current = currentIntent(intent.id, intent.generation);
    if (current && "refused" in outcome) settleFailed(current, outcome.refused, { refused: true });
    else if (current && !("refused" in outcome)) settleDone(current, outcome);
  }
  kickDrainer();
}

/** The intent as the ledger holds it now, if it is still this generation. */
function currentIntent(id: string, generation: number): MailIntent | undefined {
  const intent = getLedgerSnapshot().intents.find((i) => i.id === id);
  return intent && (intent.generation ?? 0) === (generation ?? 0) ? intent : undefined;
}

function settleError(intent: MailIntent, err: unknown): void {
  const message = err instanceof Error ? err.message : String(err);
  const kind = classifyFailure(err instanceof ApiError ? err.status : null);
  if (kind === "gone") {
    // Nothing of it reached a message that still exists.
    dropUndoFor(intent.id);
    updateIntent(intent.id, {
      state: "done", doneAt: Date.now(), notApplied: true, lastError: undefined,
      skippedIds: intent.messages.map((m) => m.id), messages: [],
    });
    return;
  }
  if (kind === "terminal" || (kind === "retry" && intent.attempts >= MAX_RETRY_ATTEMPTS)) {
    settleFailed(intent, message, { refused: kind === "terminal" });
    return;
  }
  const now = Date.now();
  updateIntent(intent.id, {
    state: "pending", notBefore: now + retryDelay(intent.attempts, kind === "network" ? "network" : "retry"),
    lastError: message,
  });
  if (kind === "network") {
    networkFailures += 1;
    networkBlockedUntil = now + retryDelay(networkFailures, "network");
  }
}

function settleDone(intent: MailIntent, outcome: Exclude<Outcome, { refused: string }>): void {
  const skipped = new Set(outcome.skippedIds);
  if (!outcome.applied) dropUndoFor(intent.id);
  updateIntent(intent.id, {
    state: "done", doneAt: Date.now(), lastError: undefined,
    notApplied: !outcome.applied,
    skippedIds: outcome.skippedIds.length > 0 ? outcome.skippedIds : undefined,
    // What the server left alone stops being shown as gone at once.
    messages: intent.messages.filter((m) => !skipped.has(m.id)),
    landedFolderId: outcome.landedFolderId,
    sources: outcome.sources?.length ? outcome.sources : undefined,
  });
}

function settleFailed(intent: MailIntent, error: string, { refused }: { refused: boolean }): void {
  // One that may have landed can still be undone.
  if (refused) dropUndoFor(intent.id);
  updateIntent(intent.id, { state: "failed", lastError: error, refused: refused || undefined });
}

/** The server answered on another channel (the event stream reconnected,
 * the browser came back online): stop waiting out backoff and try
 * everything waiting now. */
export function networkRecovered(): void {
  networkBlockedUntil = 0;
  if (!leader) return;
  const now = Date.now();
  for (const intent of getLedgerSnapshot().intents) {
    if (intent.state === "pending" && intent.notBefore > now) {
      updateIntent(intent.id, { notBefore: now });
    }
  }
  kickDrainer();
}
