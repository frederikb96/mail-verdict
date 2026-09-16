/**
 * The ledger of mail intents and the undo stack, kept in local storage.
 *
 * A module-level store rather than query or jotai state: the drainer sends
 * from outside React, a reload has to find every intent that had not been
 * sent yet, and a second tab has to see the same intents. Every change is
 * written at once, merged with whatever another tab wrote in between
 * (mergeIntents), and read back when another tab writes.
 *
 * `retired` remembers intents and undo steps removed here for a while, so a
 * copy another tab still holds does not bring them back in a merge.
 */

import {
  DONE_RETENTION_MS,
  PENDING_MARKER_DELAY_MS,
  mergeIntents,
  pruneUndo,
  type MailIntent,
  type UndoEntry,
} from "@/lib/mail-intents";
import { newIdempotencyKey } from "@/lib/idempotency-key";

const STORAGE_KEY = "mail-verdict-mail-intents";
const RETIRED_MEMORY_MS = DONE_RETENTION_MS * 2;

export interface LedgerSnapshot {
  intents: readonly MailIntent[];
  undo: readonly UndoEntry[];
}

interface StoredLedger {
  v: 1;
  intents: MailIntent[];
  undo: UndoEntry[];
  retired: Record<string, number>;
}

let snapshot: LedgerSnapshot = { intents: [], undo: [] };
let retired = new Map<string, number>();
const listeners = new Set<() => void>();
let loaded = false;

function hasStorage(): boolean {
  return typeof window !== "undefined" && typeof window.localStorage !== "undefined";
}

function readStored(): StoredLedger | null {
  if (!hasStorage()) return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredLedger;
    return parsed?.v === 1 ? parsed : null;
  } catch {
    return null;
  }
}

function mergeUndo(ours: readonly UndoEntry[], theirs: readonly UndoEntry[]): UndoEntry[] {
  const byId = new Map<string, UndoEntry>();
  for (const entry of [...theirs, ...ours]) {
    if (!retired.has(entry.id)) byId.set(entry.id, entry);
  }
  return [...byId.values()].sort((a, b) => a.createdAt - b.createdAt);
}

/** Fold another tab's copy into ours. */
function absorb(stored: StoredLedger | null): LedgerSnapshot {
  if (!stored) return snapshot;
  const now = Date.now();
  for (const [id, at] of Object.entries(stored.retired ?? {})) {
    if (now - at < RETIRED_MEMORY_MS && !retired.has(id)) retired.set(id, at);
  }
  const retiredIds = new Set(retired.keys());
  return {
    intents: mergeIntents(snapshot.intents, stored.intents ?? [], retiredIds),
    undo: pruneUndo(mergeUndo(snapshot.undo, stored.undo ?? []), now),
  };
}

function write(): void {
  if (!hasStorage()) return;
  const now = Date.now();
  for (const [id, at] of retired) if (now - at >= RETIRED_MEMORY_MS) retired.delete(id);
  snapshot = absorb(readStored());
  const doc: StoredLedger = {
    v: 1, intents: [...snapshot.intents], undo: [...snapshot.undo],
    retired: Object.fromEntries(retired),
  };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(doc));
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
    } catch {
      // Nothing more to shed; the in-memory ledger still works for this tab.
    }
  }
}

function emit(): void {
  for (const listener of listeners) listener();
}

function commit(next: LedgerSnapshot): void {
  snapshot = next;
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

const EMPTY: LedgerSnapshot = { intents: [], undo: [] };

/** The server render has no ledger. */
export function getServerLedgerSnapshot(): LedgerSnapshot {
  return EMPTY;
}

/**
 * Record intents, and optionally the undo step they make up. The intents in
 * the ledger drop their row snapshots; only the undo copy keeps them.
 */
export function addIntents(intents: MailIntent[], undoLabel?: string): UndoEntry | null {
  ensureLoaded();
  const now = Date.now();
  const stored = intents.map((i) => ({
    ...i, messages: i.messages.map(({ row: _row, ...m }) => m),
  }));
  const entry: UndoEntry | null = undoLabel
    ? { id: newIdempotencyKey(), label: undoLabel, createdAt: now, intents }
    : null;
  commit({
    intents: [...snapshot.intents, ...stored],
    undo: entry ? pruneUndo([...snapshot.undo, entry], now) : snapshot.undo,
  });
  // A fresh snapshot once the pending marker is due, so rows showing it
  // re-render without a clock of their own.
  if (intents.length > 0 && typeof window !== "undefined") {
    window.setTimeout(() => {
      snapshot = { ...snapshot };
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
  const carried = { state: updated.state, doneAt: updated.doneAt, sources: updated.sources };
  commit({
    intents: snapshot.intents.map((i) => (i.id === id ? updated : i)),
    undo: snapshot.undo.map((entry) =>
      entry.intents.some((i) => i.id === id)
        ? { ...entry, intents: entry.intents.map((i) => (i.id === id ? { ...i, ...carried } : i)) }
        : entry,
    ),
  });
  return updated;
}

/** Drop intents from the ledger for good. */
export function retireIntents(ids: readonly string[]): void {
  ensureLoaded();
  if (ids.length === 0) return;
  const now = Date.now();
  for (const id of ids) retired.set(id, now);
  const gone = new Set(ids);
  commit({ intents: snapshot.intents.filter((i) => !gone.has(i.id)), undo: snapshot.undo });
}

/** Take an undo step off the stack -- the newest, or the one named. */
export function takeUndo(entryId?: string): UndoEntry | null {
  ensureLoaded();
  const now = Date.now();
  const live = pruneUndo(snapshot.undo, now);
  const entry = entryId ? live.find((e) => e.id === entryId) : live[live.length - 1];
  if (!entry) {
    if (live.length !== snapshot.undo.length) commit({ ...snapshot, undo: live });
    return null;
  }
  retired.set(entry.id, now);
  commit({ intents: snapshot.intents, undo: live.filter((e) => e.id !== entry.id) });
  return entry;
}

/** Forget an undo step without undoing it -- its action failed. */
export function dropUndoFor(intentId: string): void {
  ensureLoaded();
  const now = Date.now();
  const doomed = snapshot.undo.filter((e) => e.intents.some((i) => i.id === intentId));
  if (doomed.length === 0) return;
  for (const entry of doomed) retired.set(entry.id, now);
  commit({ intents: snapshot.intents, undo: snapshot.undo.filter((e) => !doomed.includes(e)) });
}

/** Retire done intents past their retention and undo steps past their age. */
export function sweepLedger(now: number = Date.now()): void {
  ensureLoaded();
  const expired = snapshot.intents
    .filter((i) => i.state === "done" && now - (i.doneAt ?? now) > DONE_RETENTION_MS)
    .map((i) => i.id);
  const undo = pruneUndo(snapshot.undo, now);
  if (expired.length === 0 && undo.length === snapshot.undo.length) return;
  for (const id of expired) retired.set(id, now);
  const gone = new Set(expired);
  commit({ intents: snapshot.intents.filter((i) => !gone.has(i.id)), undo });
}
