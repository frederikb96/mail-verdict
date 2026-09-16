/**
 * The ledger of mail intents, the undo stack and the undo requests, kept in
 * local storage.
 *
 * A module-level store rather than query or jotai state: the drainer sends
 * from outside React, a reload has to find every intent that had not been
 * sent yet, and a second tab has to see the same intents. Every change is
 * written at once, merged with whatever another tab wrote in between
 * (mergeIntents), and read back when another tab writes.
 *
 * `retired` remembers records removed here for a while, so a copy another
 * tab still holds does not bring them back in a merge.
 *
 * If local storage refuses a write (the origin's quota, shared with the
 * query cache), the in-memory ledger carries on for this tab alone: a
 * stored copy it can no longer update is not read back over it, and the
 * header says actions will not survive a reload.
 */

import {
  DONE_RETENTION_MS,
  FAILED_RETENTION_MS,
  PENDING_MARKER_DELAY_MS,
  mergeIntents,
  mergeUndoEntry,
  pruneUndo,
  validIntents,
  type MailIntent,
  type UndoEntry,
  type UndoRequest,
} from "@/lib/mail-intents";
import { newIdempotencyKey } from "@/lib/idempotency-key";

const STORAGE_KEY = "mail-verdict-mail-intents";
const TAB_KEY = "mail-verdict-tab-id";
const RETIRED_MEMORY_MS = FAILED_RETENTION_MS;

export interface LedgerSnapshot {
  intents: readonly MailIntent[];
  undo: readonly UndoEntry[];
  undoRequests: readonly UndoRequest[];
  /** Local storage refused the last write. */
  persistenceFailed: boolean;
}

interface StoredLedger {
  v: 1;
  intents: MailIntent[];
  undo: UndoEntry[];
  undoRequests?: UndoRequest[];
  retired: Record<string, number>;
}

let snapshot: LedgerSnapshot = { intents: [], undo: [], undoRequests: [], persistenceFailed: false };
const retired = new Map<string, number>();
const listeners = new Set<() => void>();
let loaded = false;
let tabId: string | null = null;

function hasStorage(): boolean {
  return typeof window !== "undefined" && typeof window.localStorage !== "undefined";
}

/** This tab's id, stable across its reloads and distinct from other tabs. */
export function currentTabId(): string {
  if (tabId) return tabId;
  try {
    tabId = typeof window !== "undefined" ? window.sessionStorage.getItem(TAB_KEY) : null;
    if (!tabId) {
      tabId = newIdempotencyKey();
      if (typeof window !== "undefined") window.sessionStorage.setItem(TAB_KEY, tabId);
    }
  } catch {
    tabId = tabId ?? newIdempotencyKey();
  }
  return tabId;
}

function readStored(): StoredLedger | null {
  if (!hasStorage() || snapshot.persistenceFailed) return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredLedger> | null;
    if (!parsed || parsed.v !== 1) return null;
    return {
      v: 1,
      intents: validIntents(parsed.intents),
      undo: validEntries(parsed.undo),
      undoRequests: (Array.isArray(parsed.undoRequests) ? parsed.undoRequests : []).filter(
        (r): r is UndoRequest =>
          !!r && typeof r === "object" && typeof r.id === "string" &&
          validEntries([r.entry]).length === 1,
      ),
      retired: parsed.retired && typeof parsed.retired === "object" ? parsed.retired : {},
    };
  } catch {
    return null;
  }
}

function validEntries(value: unknown): UndoEntry[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((e): e is UndoEntry =>
      !!e && typeof e === "object" && typeof (e as UndoEntry).id === "string" &&
      typeof (e as UndoEntry).createdAt === "number")
    .map((e) => ({ ...e, intents: validIntents(e.intents) }));
}

function mergeById<T extends { id: string }>(ours: readonly T[], theirs: readonly T[]): T[] {
  const byId = new Map<string, T>();
  for (const item of [...theirs, ...ours]) {
    if (!retired.has(item.id)) byId.set(item.id, item);
  }
  return [...byId.values()];
}

function mergeUndo(ours: readonly UndoEntry[], theirs: readonly UndoEntry[]): UndoEntry[] {
  const mine = new Map(ours.map((e) => [e.id, e]));
  return mergeById(ours, theirs).map((entry) => {
    const copy = theirs.find((e) => e.id === entry.id);
    const own = mine.get(entry.id);
    return copy && own ? mergeUndoEntry(own, copy) : entry;
  });
}

/** Fold another tab's copy into ours. */
function absorb(stored: StoredLedger | null): LedgerSnapshot {
  if (!stored) return snapshot;
  const now = Date.now();
  for (const [id, at] of Object.entries(stored.retired)) {
    if (typeof at === "number" && now - at < RETIRED_MEMORY_MS && !retired.has(id)) {
      retired.set(id, at);
    }
  }
  const retiredIds = new Set(retired.keys());
  return {
    ...snapshot,
    intents: mergeIntents(snapshot.intents, stored.intents, retiredIds),
    undo: pruneUndo(
      mergeUndo(snapshot.undo, stored.undo).sort((a, b) => a.createdAt - b.createdAt), now,
    ),
    undoRequests: mergeById(snapshot.undoRequests, stored.undoRequests ?? []),
  };
}

function write(): void {
  if (!hasStorage()) return;
  const now = Date.now();
  for (const [id, at] of retired) if (now - at >= RETIRED_MEMORY_MS) retired.delete(id);
  snapshot = absorb(readStored());
  const doc: StoredLedger = {
    v: 1, intents: [...snapshot.intents], undo: [...snapshot.undo],
    undoRequests: [...snapshot.undoRequests], retired: Object.fromEntries(retired),
  };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(doc));
    snapshot = { ...snapshot, persistenceFailed: false };
  } catch {
    // Over quota: the row snapshots kept for undo are the only bulky part.
    try {
      const slim = doc.undo.map((entry) => ({
        ...entry,
        intents: entry.intents.map((i) => ({
          ...i, messages: i.messages.map(({ row: _row, ...m }) => m),
        })),
      }));
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...doc, undo: slim }));
      snapshot = { ...snapshot, persistenceFailed: false };
    } catch {
      snapshot = { ...snapshot, persistenceFailed: true };
    }
  }
}

function emit(): void {
  for (const listener of listeners) listener();
}

function commit(next: Omit<LedgerSnapshot, "persistenceFailed">): void {
  snapshot = { ...snapshot, ...next };
  write();
  emit();
}

function ensureLoaded(): void {
  if (loaded || !hasStorage()) return;
  loaded = true;
  snapshot = absorb(readStored());
  window.addEventListener("storage", (event) => {
    if (event.key !== STORAGE_KEY) return;
    snapshot = absorb(readStored());
    emit();
  });
}

export function subscribeLedger(listener: () => void): () => void {
  ensureLoaded();
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getLedgerSnapshot(): LedgerSnapshot {
  ensureLoaded();
  return snapshot;
}

const EMPTY: LedgerSnapshot = { intents: [], undo: [], undoRequests: [], persistenceFailed: false };

/** The server render has no ledger. */
export function getServerLedgerSnapshot(): LedgerSnapshot {
  return EMPTY;
}

/** The intents a screen shows: every one not waiting to be undone. */
export function projectableIntents(ledger: LedgerSnapshot): MailIntent[] {
  if (ledger.undoRequests.length === 0) return ledger.intents as MailIntent[];
  const undone = new Set(ledger.undoRequests.flatMap((r) => r.entry.intents.map((i) => i.id)));
  return ledger.intents.filter((i) => !undone.has(i.id));
}

/**
 * Record intents, and optionally the undo step they make up. The intents in
 * the ledger drop their row snapshots -- only the undo copy keeps them --
 * except reversals (`keepRows`), which show the rows they bring back.
 */
export function addIntents(
  intents: MailIntent[], undoLabel?: string, { keepRows = false }: { keepRows?: boolean } = {},
): UndoEntry | null {
  ensureLoaded();
  const now = Date.now();
  const stored = keepRows
    ? intents
    : intents.map((i) => ({ ...i, messages: i.messages.map(({ row: _row, ...m }) => m) }));
  const entry: UndoEntry | null = undoLabel
    ? { id: newIdempotencyKey(), label: undoLabel, createdAt: now, originTab: currentTabId(), intents }
    : null;
  commit({
    intents: [...snapshot.intents, ...stored],
    undo: entry ? pruneUndo([...snapshot.undo, entry], now) : snapshot.undo,
    undoRequests: snapshot.undoRequests,
  });
  // A fresh intents array once the pending marker is due, so every
  // projection re-runs and rows showing the marker re-render without a
  // clock of their own.
  if (intents.length > 0 && typeof window !== "undefined") {
    window.setTimeout(() => {
      snapshot = { ...snapshot, intents: [...snapshot.intents] };
      emit();
    }, PENDING_MARKER_DELAY_MS + 50);
  }
  return entry;
}

/** Change one intent, and its copy in any undo step. */
export function updateIntent(id: string, patch: Partial<MailIntent>): MailIntent | null {
  ensureLoaded();
  const current = snapshot.intents.find((i) => i.id === id);
  if (!current) return null;
  const updated: MailIntent = { ...current, ...patch, updatedAt: Date.now() };
  const carried: Partial<MailIntent> = {
    state: updated.state, doneAt: updated.doneAt, sources: updated.sources,
    landedFolderId: updated.landedFolderId, skippedIds: updated.skippedIds,
    notApplied: updated.notApplied, generation: updated.generation, attempts: updated.attempts,
    firstSentAt: updated.firstSentAt, updatedAt: updated.updatedAt,
  };
  commit({
    intents: snapshot.intents.map((i) => (i.id === id ? updated : i)),
    undo: snapshot.undo.map((entry) =>
      entry.intents.some((i) => i.id === id)
        ? { ...entry, intents: entry.intents.map((i) => (i.id === id ? { ...i, ...carried } : i)) }
        : entry,
    ),
    undoRequests: snapshot.undoRequests,
  });
  return updated;
}

/** Start an intent over as a new generation -- Retry on a refused one, Send
 * on a held one. `resend` keeps the record of an earlier request that may
 * have landed. */
export function restartIntent(id: string, { resend }: { resend: boolean }): void {
  const current = snapshot.intents.find((i) => i.id === id);
  if (!current) return;
  const now = Date.now();
  updateIntent(id, {
    state: "pending", generation: (current.generation ?? 0) + 1, notBefore: now,
    lastError: undefined, approvedAt: now, attempts: resend ? current.attempts : 0,
    firstSentAt: resend ? current.firstSentAt : undefined,
  });
}

/** Drop intents from the ledger for good. */
export function retireIntents(ids: readonly string[]): void {
  ensureLoaded();
  if (ids.length === 0) return;
  const now = Date.now();
  for (const id of ids) retired.set(id, now);
  const gone = new Set(ids);
  commit({
    intents: snapshot.intents.filter((i) => !gone.has(i.id)),
    undo: snapshot.undo, undoRequests: snapshot.undoRequests,
  });
}

/** Take an undo step off the stack -- the one named, or the newest this tab
 * took (`tab`). */
export function takeUndo(entryId?: string, tab?: string): UndoEntry | null {
  ensureLoaded();
  const now = Date.now();
  const live = pruneUndo(snapshot.undo, now);
  const mine = tab ? live.filter((e) => e.originTab === tab) : live;
  const entry = entryId ? live.find((e) => e.id === entryId) : mine[mine.length - 1];
  if (!entry) {
    if (live.length !== snapshot.undo.length) {
      commit({ intents: snapshot.intents, undo: live, undoRequests: snapshot.undoRequests });
    }
    return null;
  }
  retired.set(entry.id, now);
  commit({
    intents: snapshot.intents, undo: live.filter((e) => e.id !== entry.id),
    undoRequests: snapshot.undoRequests,
  });
  return entry;
}

/** Ask for an undo step to be carried out by the tab that sends. */
export function requestUndo(entry: UndoEntry): void {
  ensureLoaded();
  const request: UndoRequest = {
    id: newIdempotencyKey(), entry, requestedAt: Date.now(), originTab: currentTabId(),
  };
  commit({
    intents: snapshot.intents, undo: snapshot.undo,
    undoRequests: [...snapshot.undoRequests, request],
  });
}

/** An undo request has been carried out. */
export function consumeUndoRequest(id: string): void {
  ensureLoaded();
  retired.set(id, Date.now());
  commit({
    intents: snapshot.intents, undo: snapshot.undo,
    undoRequests: snapshot.undoRequests.filter((r) => r.id !== id),
  });
}

/** Forget an undo step without undoing it -- its action failed. */
export function dropUndoFor(intentId: string): void {
  ensureLoaded();
  const now = Date.now();
  const doomed = snapshot.undo.filter((e) => e.intents.some((i) => i.id === intentId));
  if (doomed.length === 0) return;
  for (const entry of doomed) retired.set(entry.id, now);
  commit({
    intents: snapshot.intents, undo: snapshot.undo.filter((e) => !doomed.includes(e)),
    undoRequests: snapshot.undoRequests,
  });
}

/** Retire done intents past their retention, refused and held ones nobody
 * acted on past theirs, and undo steps past their age. */
export function sweepLedger(now: number = Date.now()): void {
  ensureLoaded();
  const expired = snapshot.intents
    .filter(
      (i) =>
        (i.state === "done" && now - (i.doneAt ?? now) > DONE_RETENTION_MS) ||
        ((i.state === "failed" || i.state === "held") && now - i.updatedAt > FAILED_RETENTION_MS),
    )
    .map((i) => i.id);
  const undo = pruneUndo(snapshot.undo, now);
  if (expired.length === 0 && undo.length === snapshot.undo.length) return;
  for (const id of expired) retired.set(id, now);
  const gone = new Set(expired);
  commit({
    intents: snapshot.intents.filter((i) => !gone.has(i.id)), undo,
    undoRequests: snapshot.undoRequests,
  });
}
