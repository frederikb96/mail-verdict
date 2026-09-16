/**
 * Mail actions as durable intents, and the projection that shows them.
 *
 * Every action a person takes on mail -- archive, trash, move, spam,
 * read/unread, star, and the explicit-id form of each over a selection --
 * becomes one MailIntent before any request leaves. What a screen shows is
 * never a cache patched by hand: it is whatever the server last said, with
 * every intent the server may not have seen yet applied on top by the pure
 * functions here. So no list, conversation or count can be forgotten when a
 * new cache appears, and a read that lands while an action is still on its
 * way cannot put an archived row back.
 *
 * An intent keeps applying until the server's own data has caught up with
 * it: a cached read that began after the intent's request succeeded
 * (`doneAt`) already contains the change, so the intent no longer touches
 * it. Everything older -- a list nobody has refreshed yet, a refresh that
 * started before the write committed -- is still projected. That makes
 * retiring an intent a matter of time only (DONE_RETENTION_MS).
 *
 * No React, no network: the ledger store (intent-ledger.ts) holds these,
 * the drainer (use-intent-drainer.ts) sends them.
 */

import type { MessageSummary } from "@/types/api";

export type IntentAction =
  | "mark_read"
  | "mark_unread"
  | "flag"
  | "unflag"
  | "move"
  | "archive"
  | "trash"
  | "expunge"
  | "spam"
  | "not_spam";

/**
 * - pending: waiting to be sent (never sent, or retrying after a network
 *   error, a 5xx or a 429)
 * - inflight: a request is out
 * - done: the server answered success
 * - failed: the server refused it; nothing changed, shown until retried or
 *   dismissed
 */
export type IntentState = "pending" | "inflight" | "done" | "failed";

/** One message an intent acts on, as it looked when the action was taken. */
export interface IntentMessage {
  id: string;
  /** Where the message was -- what a folder-leaving action takes it out of.
   * Null when unknown (a row no loaded list held). */
  folderId: string | null;
  isSeen: boolean;
  isFlagged: boolean;
  threadId: string | null;
  /** The list row itself, kept so undoing a move can show it again before
   * any list has been re-read. Only kept for the first ROW_SNAPSHOT_LIMIT
   * messages of an intent. */
  row?: MessageSummary;
}

export interface MailIntent {
  /** Also the idempotency_key of the intent's request. */
  id: string;
  accountId: string;
  action: IntentAction;
  targetFolderId?: string;
  messages: IntentMessage[];
  /** Sent to the bulk endpoint rather than the single-message one. */
  bulk: boolean;
  /** Each id stands for its whole conversation in its folder. */
  expandThreads?: boolean;
  createdAt: number;
  state: IntentState;
  attempts: number;
  /** Earliest time the next attempt may be sent. */
  notBefore: number;
  lastError?: string;
  doneAt?: number;
  /** Every message a conversation-expanded bulk action acted on, from its
   * response -- what undoing it moves back. */
  sources?: Array<{ id: string; folderId: string }>;
  /** Undo was asked for while the request was out; the intent no longer
   * projects, and once it settles it is reversed or dropped. */
  undoRequested?: boolean;
  /** The intent this one reverses. Sent only once that one has settled. */
  reverses?: string;
  /** Last change to this record, for merging copies held by two tabs. */
  updatedAt: number;
}

/** A step the person can take back: one user action, which may have been
 * sent as several intents (a selection spanning accounts). The intents are
 * copied here, since a done intent is retired from the ledger long before
 * its undo entry expires. */
export interface UndoEntry {
  id: string;
  label: string;
  createdAt: number;
  intents: MailIntent[];
}

/** Human phrasing for an action, as in "Could not archive". */
export const ACTION_LABELS: Record<IntentAction, string> = {
  mark_read: "mark as read",
  mark_unread: "mark as unread",
  flag: "star",
  unflag: "unstar",
  move: "move",
  archive: "archive",
  trash: "move to trash",
  expunge: "delete forever",
  spam: "mark as spam",
  not_spam: "mark as not spam",
};

/** How long a done intent keeps projecting over caches read before it. */
export const DONE_RETENTION_MS = 10 * 60_000;
/** How long a refused intent stays on its row waiting for Retry or Discard. */
export const FAILED_RETENTION_MS = 24 * 60 * 60_000;
/** Undo steps kept, newest last. */
export const UNDO_STACK_LIMIT = 20;
/** An undo step older than this is dropped rather than applied. */
export const UNDO_MAX_AGE_MS = 30 * 60_000;
/** A pending intent shows its marker only once it is this old. */
export const PENDING_MARKER_DELAY_MS = 800;
/** Row snapshots kept per intent -- enough for any selection a person
 * looks at, bounded so a huge selection cannot fill local storage. */
export const ROW_SNAPSHOT_LIMIT = 25;

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30_000;
const NETWORK_RETRY_MAX_MS = 10_000;

/** Actions that take a message out of the folder it is in. */
const LEAVING_ACTIONS: ReadonlySet<IntentAction> = new Set([
  "move", "archive", "trash", "expunge", "spam", "not_spam",
]);

export function leavesFolder(action: IntentAction): boolean {
  return LEAVING_ACTIONS.has(action);
}

/** Actions a person can take back. Expunge has nothing left to restore. */
export function isUndoable(action: IntentAction): boolean {
  return action !== "expunge";
}

/** Whether an intent still changes what is shown. A failed intent changed
 * nothing on the server, so the server's data is shown as it is. */
export function isProjecting(intent: MailIntent): boolean {
  return intent.state !== "failed" && !intent.undoRequested;
}

/** Whether an intent's effect may be missing from data read at `dataUpdatedAt`. */
function appliesTo(intent: MailIntent, dataUpdatedAt: number): boolean {
  if (!isProjecting(intent)) return false;
  return intent.state !== "done" || (intent.doneAt ?? 0) > dataUpdatedAt;
}

function byCreation(a: MailIntent, b: MailIntent): number {
  return a.createdAt - b.createdAt || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

interface RowLike {
  id: string;
  folder_id: string;
  thread_id: string;
  is_seen: boolean;
  is_flagged: boolean;
  received_at: string | null;
  unread_in_thread?: number;
}

/** What a projected row carries beyond the server's fields. */
export interface RowIntentMarks {
  /** An action on this row the server has not confirmed, and since when. */
  pendingSince?: number;
  /** The newest failed intent naming this row. */
  failedIntent?: { id: string; action: IntentAction; error: string };
}

function findMessage(intent: MailIntent, id: string): IntentMessage | undefined {
  return intent.messages.find((m) => m.id === id);
}

/** Whether `intent` takes `row` out of a list that shows it. */
function hidesRow(intent: MailIntent, row: RowLike): boolean {
  if (!leavesFolder(intent.action)) return false;
  const named = findMessage(intent, row.id);
  const conversationMember =
    !named &&
    intent.expandThreads === true &&
    intent.messages.some((m) => m.threadId === row.thread_id && m.folderId === row.folder_id);
  if (!named && !conversationMember) return false;
  if (intent.action === "expunge") return true;
  if (intent.action === "move" && intent.targetFolderId) {
    return row.folder_id !== intent.targetFolderId;
  }
  const origin = named ? named.folderId : row.folder_id;
  return origin === null || row.folder_id === origin;
}

function flagOverride(action: IntentAction): Partial<Pick<RowLike, "is_seen" | "is_flagged">> {
  switch (action) {
    case "mark_read":
      return { is_seen: true };
    case "mark_unread":
      return { is_seen: false };
    case "flag":
      return { is_flagged: true };
    case "unflag":
      return { is_flagged: false };
    default:
      return {};
  }
}

/** Newest first, the server's own order: received_at DESC (NULL first), id DESC. */
function sitsAbove(a: RowLike, b: RowLike): boolean {
  if (a.received_at !== b.received_at) {
    if (a.received_at === null) return true;
    if (b.received_at === null) return false;
    const at = Date.parse(a.received_at);
    const bt = Date.parse(b.received_at);
    if (at !== bt) return at > bt;
  }
  return a.id > b.id;
}

export interface ListProjection {
  /** When the rows were read -- see the module comment. */
  dataUpdatedAt: number;
  /** The folders the list covers: one for a folder, several for a unified
   * view. A move back into one of them shows its row again. Null for a
   * list whose scope is unknown, which only ever loses rows. */
  scopeFolderIds: ReadonlySet<string> | null;
  /** Whether the list holds one row per conversation. */
  threaded: boolean;
  /** Whether rows exist past the last one loaded -- a restored row sorting
   * below the loaded window is left for paging to find. */
  hasMore: boolean;
}

/**
 * The rows a list shows: server rows with every applicable intent on top.
 * Returns `rows` itself when no intent touches them, so a memo keyed on
 * the result stays stable.
 */
export function projectRows<T extends RowLike>(
  rows: readonly T[],
  intents: readonly MailIntent[],
  list: ListProjection,
): Array<T & RowIntentMarks> {
  const relevant = intents.filter((i) => isProjecting(i) || i.state === "failed");
  if (relevant.length === 0) return rows as Array<T & RowIntentMarks>;
  const ordered = [...relevant].sort(byCreation);

  let changed = false;
  const out: Array<T & RowIntentMarks> = [];
  const present = new Set<string>();
  const presentThreads = new Set<string>();
  for (const row of rows) {
    let next: T & RowIntentMarks = row;
    let hidden = false;
    for (const intent of ordered) {
      const named = findMessage(intent, row.id);
      if (intent.state === "failed") {
        if (named) next = { ...next, failedIntent: failureOf(intent) };
        continue;
      }
      if (!appliesTo(intent, list.dataUpdatedAt)) continue;
      if (hidesRow(intent, next)) {
        hidden = true;
        break;
      }
      if (named) {
        const override = flagOverride(intent.action);
        if (Object.keys(override).length > 0) next = { ...next, ...override };
        if (intent.state !== "done") next = markPending(next, intent);
      }
    }
    if (!hidden && list.threaded && next.unread_in_thread !== undefined) {
      const delta = conversationUnreadDelta(next, ordered, list);
      if (delta !== 0) {
        next = { ...next, unread_in_thread: Math.max(0, next.unread_in_thread! + delta) };
      }
    }
    if (hidden) {
      changed = true;
      continue;
    }
    if (next !== row) changed = true;
    out.push(next);
    present.add(next.id);
    presentThreads.add(next.thread_id);
  }

  const restored = restoredRows<T>(ordered, list, present, presentThreads);
  if (restored.length > 0) {
    const last = out[out.length - 1];
    for (const row of restored) {
      if (list.hasMore && last && !sitsAbove(row, last)) continue;
      const at = out.findIndex((existing) => sitsAbove(row, existing));
      out.splice(at < 0 ? out.length : at, 0, row as T & RowIntentMarks);
      changed = true;
    }
  }

  return changed ? out : (rows as Array<T & RowIntentMarks>);
}

function markPending<T extends RowIntentMarks>(row: T, intent: MailIntent): T {
  if (row.pendingSince !== undefined && row.pendingSince <= intent.createdAt) return row;
  return { ...row, pendingSince: intent.createdAt };
}

function failureOf(intent: MailIntent): NonNullable<RowIntentMarks["failedIntent"]> {
  return { id: intent.id, action: intent.action, error: intent.lastError ?? "" };
}

/**
 * Rows a pending move brings into this list -- an undo moving a message back
 * into a folder the list covers -- taken from the row snapshot the original
 * action kept, and only where the list does not already hold it.
 */
function restoredRows<T extends RowLike>(
  ordered: readonly MailIntent[],
  list: ListProjection,
  present: ReadonlySet<string>,
  presentThreads: ReadonlySet<string>,
): T[] {
  if (!list.scopeFolderIds) return [];
  const restored = new Map<string, T>();
  for (const intent of ordered) {
    if (!appliesTo(intent, list.dataUpdatedAt)) continue;
    if (intent.action === "move" && intent.targetFolderId && list.scopeFolderIds.has(intent.targetFolderId)) {
      for (const m of intent.messages) {
        if (!m.row || present.has(m.id)) continue;
        if (list.threaded && m.threadId && presentThreads.has(m.threadId)) continue;
        restored.set(m.id, { ...m.row, folder_id: intent.targetFolderId } as unknown as T);
      }
      continue;
    }
    for (const m of intent.messages) {
      const row = restored.get(m.id);
      if (!row) continue;
      if (hidesRow(intent, row)) {
        restored.delete(m.id);
      } else {
        restored.set(m.id, { ...row, ...flagOverride(intent.action) });
      }
    }
  }
  return [...restored.values()];
}

/** Per message: where it is and whether it is read, walking the intents in
 * the order they were taken, starting from what the first one saw. */
interface MessageTrack {
  folderId: string | null;
  isSeen: boolean;
}

/** Apply one intent to a message's tracked state, reporting the count
 * changes it causes in the folders involved. */
function stepMessage(
  intent: MailIntent,
  track: MessageTrack,
  emit: (folderId: string, total: number, unread: number) => void,
): void {
  const { action } = intent;
  if (leavesFolder(action)) {
    if (track.folderId !== null) emit(track.folderId, -1, track.isSeen ? 0 : -1);
    if (action === "move" && intent.targetFolderId) {
      emit(intent.targetFolderId, 1, track.isSeen ? 0 : 1);
      track.folderId = intent.targetFolderId;
    } else {
      track.folderId = null;
    }
    return;
  }
  if (action === "mark_read" && !track.isSeen) {
    if (track.folderId !== null) emit(track.folderId, 0, -1);
    track.isSeen = true;
  } else if (action === "mark_unread" && track.isSeen) {
    if (track.folderId !== null) emit(track.folderId, 0, 1);
    track.isSeen = false;
  }
}

function walkMessages(
  ordered: readonly MailIntent[],
  visit: (intent: MailIntent, message: IntentMessage, track: MessageTrack) => void,
): void {
  const tracks = new Map<string, MessageTrack>();
  for (const intent of ordered) {
    if (!isProjecting(intent)) continue;
    for (const message of intent.messages) {
      let track = tracks.get(message.id);
      if (!track) {
        track = { folderId: message.folderId, isSeen: message.isSeen };
        tracks.set(message.id, track);
      }
      visit(intent, message, track);
    }
  }
}

function conversationUnreadDelta(
  row: RowLike,
  ordered: readonly MailIntent[],
  list: ListProjection,
): number {
  let delta = 0;
  const inScope = (folderId: string) =>
    list.scopeFolderIds ? list.scopeFolderIds.has(folderId) : folderId === row.folder_id;
  walkMessages(ordered, (intent, message, track) => {
    const counts = message.threadId === row.thread_id && appliesTo(intent, list.dataUpdatedAt);
    stepMessage(intent, track, (folderId, _total, unread) => {
      if (counts && inScope(folderId)) delta += unread;
    });
  });
  return delta;
}

/** Count changes per folder not yet in counts read at `dataUpdatedAt`. */
export function folderCountDeltas(
  intents: readonly MailIntent[],
  dataUpdatedAt: number,
): Map<string, { total: number; unread: number }> {
  const deltas = new Map<string, { total: number; unread: number }>();
  walkMessages([...intents].sort(byCreation), (intent, message, track) => {
    const counts = appliesTo(intent, dataUpdatedAt);
    stepMessage(intent, track, (folderId, total, unread) => {
      if (!counts) return;
      const d = deltas.get(folderId) ?? { total: 0, unread: 0 };
      d.total += total;
      d.unread += unread;
      deltas.set(folderId, d);
    });
  });
  return deltas;
}

/** Apply folder count deltas to anything carrying a folder id and counts. */
export function projectCounts<T extends { unread_count: number; total_count: number }>(
  item: T,
  folderIds: readonly string[],
  deltas: ReadonlyMap<string, { total: number; unread: number }>,
): T {
  let total = 0;
  let unread = 0;
  for (const id of folderIds) {
    const d = deltas.get(id);
    if (!d) continue;
    total += d.total;
    unread += d.unread;
  }
  if (total === 0 && unread === 0) return item;
  return {
    ...item,
    total_count: Math.max(0, item.total_count + total),
    unread_count: Math.max(0, item.unread_count + unread),
  };
}

/** A conversation's messages with every applicable flag and move on top.
 * Messages are never hidden here: a conversation spans folders. */
export function projectThreadMessages<T extends RowLike>(
  messages: readonly T[],
  intents: readonly MailIntent[],
  dataUpdatedAt: number,
): Array<T & RowIntentMarks> {
  const ordered = intents.filter((i) => appliesTo(i, dataUpdatedAt)).sort(byCreation);
  if (ordered.length === 0) return messages as Array<T & RowIntentMarks>;
  let changed = false;
  const out = messages.map((message) => {
    let next: T & RowIntentMarks = message;
    for (const intent of ordered) {
      if (!findMessage(intent, message.id)) continue;
      next = { ...next, ...flagOverride(intent.action) };
      if (intent.action === "move" && intent.targetFolderId) {
        next = { ...next, folder_id: intent.targetFolderId };
      }
      if (intent.state !== "done") next = markPending(next, intent);
    }
    if (next !== message) changed = true;
    return next;
  });
  return changed ? out : (messages as Array<T & RowIntentMarks>);
}

// --- Sending ---------------------------------------------------------------

export type FailureKind = "retry" | "network" | "terminal" | "gone";

/**
 * What a failed request means for its intent: a network error or timeout
 * waits for the network, a 408/429/5xx retries with backoff, a 404 means
 * the message is gone and the intent is retired without a word, and any
 * other 4xx is a refusal shown to the person.
 */
export function classifyFailure(status: number | null): FailureKind {
  if (status === null) return "network";
  if (status === 404) return "gone";
  if (status === 408 || status === 429 || status >= 500) return "retry";
  return "terminal";
}

/** Backoff before attempt `attempts + 1`. A dead connection fails fast, so
 * retrying it costs nothing and is capped lower than a busy server. */
export function retryDelay(attempts: number, kind: "retry" | "network" = "retry"): number {
  const cap = kind === "network" ? NETWORK_RETRY_MAX_MS : RETRY_MAX_MS;
  return Math.min(RETRY_BASE_MS * 2 ** Math.max(0, attempts - 1), cap);
}

/**
 * The intents that may be sent now, at most one per account, each the
 * earliest sendable one of its account. An intent waits behind any earlier
 * unsettled intent naming one of its messages (so a mark-read and an
 * archive of the same message arrive in order), and a reversing intent
 * waits for the one it reverses.
 */
export function sendableIntents(intents: readonly MailIntent[], now: number): MailIntent[] {
  const ordered = [...intents].sort(byCreation);
  const busyAccounts = new Set(ordered.filter((i) => i.state === "inflight").map((i) => i.accountId));
  const unsettled = new Set(
    ordered.filter((i) => i.state === "pending" || i.state === "inflight").map((i) => i.id),
  );
  const out: MailIntent[] = [];
  const claimedMessages = new Set<string>();
  for (const intent of ordered) {
    const unsettledHere = intent.state === "pending" || intent.state === "inflight";
    if (!unsettledHere) continue;
    const blocked =
      intent.state !== "pending" ||
      busyAccounts.has(intent.accountId) ||
      intent.notBefore > now ||
      (intent.reverses !== undefined && unsettled.has(intent.reverses)) ||
      intent.messages.some((m) => claimedMessages.has(m.id));
    for (const m of intent.messages) claimedMessages.add(m.id);
    if (blocked) continue;
    out.push(intent);
    busyAccounts.add(intent.accountId);
  }
  return out;
}

/** The earliest time a waiting intent becomes sendable, if any is waiting. */
export function nextWakeAt(intents: readonly MailIntent[], now: number): number | null {
  let wake: number | null = null;
  for (const intent of intents) {
    if (intent.state !== "pending" || intent.notBefore <= now) continue;
    wake = wake === null ? intent.notBefore : Math.min(wake, intent.notBefore);
  }
  return wake;
}

// --- Undo ------------------------------------------------------------------

type NewId = () => string;

/**
 * The intents that put an already-applied intent back: a move to wherever
 * each message came from (and unread again where it was unread), or the
 * opposite flag for each message whose flag it changed.
 */
export function reversalsOf(original: MailIntent, now: number, newId: NewId): MailIntent[] {
  const base = {
    accountId: original.accountId, bulk: true, createdAt: now, state: "pending" as const,
    attempts: 0, notBefore: now, reverses: original.id, updatedAt: now,
  };
  const snapshots = new Map(original.messages.map((m) => [m.id, m]));

  if (leavesFolder(original.action)) {
    const sources = original.sources?.length
      ? original.sources
      : original.messages.flatMap((m) => (m.folderId ? [{ id: m.id, folderId: m.folderId }] : []));
    const byFolder = new Map<string, IntentMessage[]>();
    const unread: IntentMessage[] = [];
    for (const source of sources) {
      const snapshot = snapshots.get(source.id);
      const message: IntentMessage = {
        id: source.id,
        folderId: original.action === "move" ? original.targetFolderId ?? null : null,
        isSeen: snapshot?.isSeen ?? true,
        isFlagged: snapshot?.isFlagged ?? false,
        threadId: snapshot?.threadId ?? null,
        row: snapshot?.row,
      };
      const group = byFolder.get(source.folderId) ?? [];
      group.push(message);
      byFolder.set(source.folderId, group);
      if (snapshot && !snapshot.isSeen) unread.push({ ...message, folderId: source.folderId });
    }
    const moves: MailIntent[] = [...byFolder.entries()].map(([folderId, messages]) => ({
      ...base, id: newId(), action: "move", targetFolderId: folderId, messages,
    }));
    if (unread.length === 0) return moves;
    // Created a moment later than the moves, so it is sent after them.
    return [
      ...moves,
      { ...base, id: newId(), createdAt: now + 1, action: "mark_unread", messages: unread },
    ];
  }

  const inverse: Partial<Record<IntentAction, [IntentAction, (m: IntentMessage) => boolean]>> = {
    mark_read: ["mark_unread", (m) => !m.isSeen],
    mark_unread: ["mark_read", (m) => m.isSeen],
    flag: ["unflag", (m) => !m.isFlagged],
    unflag: ["flag", (m) => m.isFlagged],
  };
  const pair = inverse[original.action];
  if (!pair) return [];
  const messages = original.messages.filter(pair[1]);
  if (messages.length === 0) return [];
  return [{ ...base, id: newId(), action: pair[0], messages }];
}

/** Undo steps still worth offering: young enough, newest last, bounded. */
export function pruneUndo(entries: readonly UndoEntry[], now: number): UndoEntry[] {
  const young = entries.filter((e) => now - e.createdAt <= UNDO_MAX_AGE_MS);
  return young.slice(Math.max(0, young.length - UNDO_STACK_LIMIT));
}

// --- Two tabs --------------------------------------------------------------

/** A settled intent never goes back to unsettled; between two copies at
 * the same rank the later change wins. Pending and inflight share a rank so
 * a tab taking over sending can turn an inflight copy left by a closed tab
 * back into pending. */
const STATE_RANK: Record<IntentState, number> = { pending: 0, inflight: 0, done: 1, failed: 1 };

/**
 * Two copies of the ledger, one per tab, merged: every intent either holds
 * that has not been retired, the more advanced copy of each winning. An undo
 * asked for in either tab sticks.
 */
export function mergeIntents(
  ours: readonly MailIntent[],
  theirs: readonly MailIntent[],
  retired: ReadonlySet<string>,
): MailIntent[] {
  const merged = new Map<string, MailIntent>();
  for (const intent of [...ours, ...theirs]) {
    if (retired.has(intent.id)) continue;
    const held = merged.get(intent.id);
    if (!held) {
      merged.set(intent.id, intent);
      continue;
    }
    const rank = STATE_RANK[intent.state] - STATE_RANK[held.state];
    const winner = rank > 0 || (rank === 0 && intent.updatedAt > held.updatedAt) ? intent : held;
    const undoRequested = held.undoRequested || intent.undoRequested;
    merged.set(intent.id, undoRequested ? { ...winner, undoRequested } : winner);
  }
  return [...merged.values()].sort(byCreation);
}
