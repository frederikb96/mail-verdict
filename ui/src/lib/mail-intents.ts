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
 * Every projection takes the moment its data was *read* (the request's
 * start, see read-clock.ts): data read after an intent's request succeeded
 * (`doneAt`) already holds the change and is shown as the server has it.
 * Counts are stricter, since a count cannot absorb a change twice the way a
 * hidden row can: they take an intent only into data read before its first
 * request left (`firstSentAt`), because a request whose answer was lost may
 * already have been counted by the server.
 *
 * Every request is guarded to the folder the person saw each message in, so
 * an intent sent late -- after a reconnect, a reload, an hour asleep -- or an
 * undo never pulls a message out of wherever it has been filed since.
 *
 * No React, no network: the ledger (intent-ledger.ts) holds these, the
 * drainer (intent-drainer.ts) sends them.
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
 * - pending: waiting to be sent (never sent, or retrying)
 * - inflight: a request is out
 * - done: the server answered; see `notApplied`
 * - failed: the server refused it; nothing changed, shown until retried or
 *   discarded
 * - held: waited so long unsent that it is no longer sent without the
 *   person confirming it (PENDING_TTL_MS)
 */
export type IntentState = "pending" | "inflight" | "done" | "failed" | "held";

/** One message an intent acts on, as it looked when the action was taken. */
export interface IntentMessage {
  id: string;
  /** Where the message was -- what the request is guarded to, and what a
   * folder-leaving action takes it out of. Null when unknown. */
  folderId: string | null;
  isSeen: boolean;
  isFlagged: boolean;
  threadId: string | null;
  /** When the newest list showing it was read (the page's `as_of`, the
   * server's clock), for a conversation not to sweep in replies that
   * arrived later. */
  listedAt?: string | null;
  /** The list row itself, kept on an undo step's copy so undoing a move can
   * show it again before any list has been re-read. */
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
  /** When the first request of the current generation left. */
  firstSentAt?: number;
  doneAt?: number;
  /** The server wrote nothing: every message had moved or was gone. */
  notApplied?: boolean;
  /** Messages the server left alone because they had moved or were gone. */
  skippedIds?: string[];
  /** Where the server filed the messages -- where an undo expects them. */
  landedFolderId?: string;
  /** Every message a conversation-expanded bulk action acted on, from its
   * response -- what undoing it moves back. */
  sources?: Array<{ id: string; folderId: string }>;
  /** The intent this one reverses. */
  reverses?: string;
  /** When the person confirmed sending a held intent. */
  approvedAt?: number;
  /** Bumped by Retry or Send: a newer generation outranks any copy of the
   * older one another tab still holds. */
  generation: number;
  /** The tab that took the action, which is where its outcome is told. */
  originTab?: string;
  /** Last change to this record, for merging copies held by two tabs. */
  updatedAt: number;
}

/** A step the person can take back: one user action, which may have been
 * sent as several intents (a selection spanning accounts). The intents are
 * copied here, since a done intent is retired from the ledger long before
 * its undo step expires. */
export interface UndoEntry {
  id: string;
  label: string;
  createdAt: number;
  originTab?: string;
  intents: MailIntent[];
}

/** An undo asked for in some tab, carried out by the tab that sends --
 * which can see whether each intent has been answered yet. */
export interface UndoRequest {
  id: string;
  entry: UndoEntry;
  requestedAt: number;
  originTab?: string;
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
/** How long a refused or held intent waits for Retry, Send or Discard. */
export const FAILED_RETENTION_MS = 24 * 60 * 60_000;
/** An intent unsent this long is held for the person to confirm: the
 * mailbox may have changed in ways the request's guard cannot see. */
export const PENDING_TTL_MS = 60 * 60_000;
/** Attempts against a server answering 5xx/429 before giving up visibly. */
export const MAX_RETRY_ATTEMPTS = 8;
/** Undo steps kept, newest last. */
export const UNDO_STACK_LIMIT = 20;
/** An undo step older than this is dropped rather than applied. */
export const UNDO_MAX_AGE_MS = 30 * 60_000;
/** A pending intent shows its marker only once it is this old. */
export const PENDING_MARKER_DELAY_MS = 800;
/** Row snapshots kept per undo step -- enough for any selection a person
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

/** Whether an intent still changes what is shown. A failed or held intent
 * changed nothing on the server, so the server's data is shown as it is. */
export function isProjecting(intent: MailIntent): boolean {
  return intent.state === "pending" || intent.state === "inflight" || intent.state === "done";
}

/** Whether an intent's effect may be missing from rows read at `readAt`. */
export function appliesTo(intent: MailIntent, readAt: number): boolean {
  if (!isProjecting(intent)) return false;
  return intent.state !== "done" || (intent.doneAt ?? 0) > readAt;
}

/** Whether a count read at `readAt` can still be missing the intent: only
 * if it was read before any request of it left. */
function countsApply(intent: MailIntent, readAt: number): boolean {
  return isProjecting(intent) && readAt < (intent.firstSentAt ?? Number.POSITIVE_INFINITY);
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

/** Intents indexed for one projection pass. */
interface IntentIndex {
  byMessage: Map<string, { intent: MailIntent; message: IntentMessage }[]>;
  /** Conversation-expanded leaving intents, by `thread|folder` of an anchor. */
  byConversation: Map<string, MailIntent[]>;
}

function indexIntents(ordered: readonly MailIntent[]): IntentIndex {
  const byMessage: IntentIndex["byMessage"] = new Map();
  const byConversation: IntentIndex["byConversation"] = new Map();
  for (const intent of ordered) {
    for (const message of intent.messages) {
      const list = byMessage.get(message.id);
      if (list) list.push({ intent, message });
      else byMessage.set(message.id, [{ intent, message }]);
      if (intent.expandThreads && message.threadId && message.folderId) {
        const key = `${message.threadId}|${message.folderId}`;
        const convo = byConversation.get(key);
        if (!convo) byConversation.set(key, [intent]);
        else if (!convo.includes(intent)) convo.push(intent);
      }
    }
  }
  return { byMessage, byConversation };
}

/** Whether `intent` takes a row out of a list that shows it -- a row it
 * names, however stale the folder the list holds for it, or a member of a
 * conversation it expanded (found by thread and folder, see IntentIndex).
 * A move leaves only lists outside its target. */
function hidesRow(intent: MailIntent, row: RowLike): boolean {
  if (!leavesFolder(intent.action)) return false;
  if (intent.action === "move" && intent.targetFolderId) {
    return row.folder_id !== intent.targetFolderId;
  }
  return true;
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
  readAt: number;
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
 * the result stays stable. Linear in rows plus intent messages.
 */
export function projectRows<T extends RowLike>(
  rows: readonly T[],
  intents: readonly MailIntent[],
  list: ListProjection,
): Array<T & RowIntentMarks> {
  const applying = intents.filter((i) => appliesTo(i, list.readAt)).sort(byCreation);
  const failed = intents.filter((i) => i.state === "failed");
  if (applying.length === 0 && failed.length === 0) return rows as Array<T & RowIntentMarks>;

  const index = indexIntents(applying);
  const failures = new Map<string, MailIntent>();
  for (const intent of [...failed].sort(byCreation)) {
    for (const m of intent.messages) failures.set(m.id, intent);
  }
  const unreadDeltas = list.threaded ? conversationUnreadDeltas(intents, list.readAt) : null;

  let changed = false;
  const out: Array<T & RowIntentMarks> = [];
  const present = new Set<string>();
  const presentThreads = new Set<string>();
  for (const row of rows) {
    let next: T & RowIntentMarks = row;
    let hidden = false;
    for (const { intent } of index.byMessage.get(row.id) ?? []) {
      if (hidesRow(intent, next)) {
        hidden = true;
        break;
      }
      const override = flagOverride(intent.action);
      if (Object.keys(override).length > 0) next = { ...next, ...override };
      if (intent.state !== "done") next = markPending(next, intent);
    }
    if (!hidden) {
      for (const intent of index.byConversation.get(`${row.thread_id}|${row.folder_id}`) ?? []) {
        if (hidesRow(intent, next)) {
          hidden = true;
          break;
        }
      }
    }
    if (hidden) {
      changed = true;
      continue;
    }
    const failure = failures.get(row.id);
    if (failure) {
      next = {
        ...next,
        failedIntent: { id: failure.id, action: failure.action, error: failure.lastError ?? "" },
      };
    }
    if (unreadDeltas && next.unread_in_thread !== undefined) {
      const byFolder = unreadDeltas.get(next.thread_id);
      let delta = 0;
      if (byFolder) {
        for (const [folderId, d] of byFolder) {
          const inScope = list.scopeFolderIds
            ? list.scopeFolderIds.has(folderId)
            : folderId === next.folder_id;
          if (inScope) delta += d;
        }
      }
      if (delta !== 0) {
        next = { ...next, unread_in_thread: Math.max(0, next.unread_in_thread! + delta) };
      }
    }
    if (next !== row) changed = true;
    out.push(next);
    present.add(next.id);
    presentThreads.add(next.thread_id);
  }

  const restored = restoredRows<T>(applying, list, present, presentThreads);
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

/**
 * Rows a pending move brings into this list -- an undo moving a message back
 * into a folder the list covers -- from the row snapshot the undo step kept,
 * and only where the list does not already hold it.
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
    if (
      intent.action === "move" && intent.targetFolderId &&
      list.scopeFolderIds.has(intent.targetFolderId)
    ) {
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
      if (hidesRow(intent, row)) restored.delete(m.id);
      else restored.set(m.id, { ...row, ...flagOverride(intent.action) });
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
    const landed = action === "move" ? intent.targetFolderId : intent.landedFolderId;
    if (landed && action !== "expunge") {
      emit(landed, 1, track.isSeen ? 0 : 1);
      track.folderId = landed;
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

/** Walk every projecting intent's messages once, in the order taken. */
function walkMessages(
  intents: readonly MailIntent[],
  visit: (intent: MailIntent, message: IntentMessage, track: MessageTrack) => void,
): void {
  const tracks = new Map<string, MessageTrack>();
  for (const intent of [...intents].filter(isProjecting).sort(byCreation)) {
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

/** Unread count changes per conversation and folder not yet in a list read
 * at `readAt`. */
function conversationUnreadDeltas(
  intents: readonly MailIntent[],
  readAt: number,
): Map<string, Map<string, number>> {
  const deltas = new Map<string, Map<string, number>>();
  walkMessages(intents, (intent, message, track) => {
    const counts = message.threadId !== null && countsApply(intent, readAt);
    stepMessage(intent, track, (folderId, _total, unread) => {
      if (!counts || unread === 0) return;
      const byFolder = deltas.get(message.threadId!) ?? new Map<string, number>();
      byFolder.set(folderId, (byFolder.get(folderId) ?? 0) + unread);
      deltas.set(message.threadId!, byFolder);
    });
  });
  return deltas;
}

/** Count changes per folder not yet in counts read at `readAt`. */
export function folderCountDeltas(
  intents: readonly MailIntent[],
  readAt: number,
): Map<string, { total: number; unread: number }> {
  const deltas = new Map<string, { total: number; unread: number }>();
  walkMessages(intents, (intent, _message, track) => {
    const counts = countsApply(intent, readAt);
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
  readAt: number,
): Array<T & RowIntentMarks> {
  const applying = intents.filter((i) => appliesTo(i, readAt)).sort(byCreation);
  if (applying.length === 0) return messages as Array<T & RowIntentMarks>;
  const { byMessage } = indexIntents(applying);
  let changed = false;
  const out = messages.map((message) => {
    let next: T & RowIntentMarks = message;
    for (const { intent } of byMessage.get(message.id) ?? []) {
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
 * waits for the network, a 408/425/429/5xx retries with backoff, a 404 means
 * the message is gone and the intent ends without a word, and any other 4xx
 * is a refusal shown to the person.
 */
export function classifyFailure(status: number | null): FailureKind {
  if (status === null) return "network";
  if (status === 404) return "gone";
  if (status === 408 || status === 425 || status === 429 || status >= 500) return "retry";
  return "terminal";
}

/** Backoff before attempt `attempts + 1`. A dead connection fails fast, so
 * retrying it costs nothing and is capped lower than a busy server. */
export function retryDelay(attempts: number, kind: "retry" | "network" = "retry"): number {
  const cap = kind === "network" ? NETWORK_RETRY_MAX_MS : RETRY_MAX_MS;
  return Math.min(RETRY_BASE_MS * 2 ** Math.max(0, attempts - 1), cap);
}

/** Pending intents unsent past PENDING_TTL_MS -- to be held. */
export function staleIntents(intents: readonly MailIntent[], now: number): MailIntent[] {
  return intents.filter(
    (i) => i.state === "pending" && now - (i.approvedAt ?? i.createdAt) > PENDING_TTL_MS,
  );
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
  const unsettledStates = (i: MailIntent) =>
    i.state === "pending" || i.state === "inflight" || i.state === "held";
  const busyAccounts = new Set(ordered.filter((i) => i.state === "inflight").map((i) => i.accountId));
  const unsettled = new Set(ordered.filter(unsettledStates).map((i) => i.id));
  const out: MailIntent[] = [];
  const claimedMessages = new Set<string>();
  for (const intent of ordered) {
    if (!unsettledStates(intent)) continue;
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

/**
 * The request body guards for an intent: where each message was seen, and
 * how recent a conversation member may be -- no later than the list it was
 * acted on from was read.
 *
 * Only an action that files messages somewhere, or destroys them, is
 * guarded to a folder -- and every reversal, whose whole point is where the
 * original left things. A flag set by the reader follows the message
 * wherever another client has filed it: nothing is lost by that, and the
 * reader asked for it.
 */
export function requestGuards(intent: MailIntent): {
  expectedFolderIds: Record<string, string>;
  expandThreadsThrough: string | null;
} {
  const guarded = leavesFolder(intent.action) || intent.reverses !== undefined;
  const expectedFolderIds: Record<string, string> = {};
  let newest: string | null = null;
  for (const m of intent.messages) {
    if (guarded && m.folderId) expectedFolderIds[m.id] = m.folderId;
    if (m.listedAt && (newest === null || Date.parse(m.listedAt) > Date.parse(newest))) {
      newest = m.listedAt;
    }
  }
  return { expectedFolderIds, expandThreadsThrough: intent.expandThreads ? newest : null };
}

// --- Undo ------------------------------------------------------------------

type NewId = () => string;

/**
 * The intents that put an applied intent back, each guarded to where the
 * original left its messages: a move back to the folder each came from (and
 * unread again where it was unread), or the opposite flag for each message
 * whose flag it changed. A message moved on since is left where it is.
 *
 * Nothing for an intent that wrote nothing, and nothing for a leaving
 * action whose landing folder is unknown -- guessing would be the
 * unguarded move back this exists to avoid.
 */
export function reversalsOf(original: MailIntent, now: number, newId: NewId): MailIntent[] {
  if (original.notApplied || original.state !== "done") return [];
  const base = {
    accountId: original.accountId, bulk: true, createdAt: now, state: "pending" as const,
    attempts: 0, notBefore: now, reverses: original.id, updatedAt: now, generation: 0,
    originTab: original.originTab,
  };
  const skipped = new Set(original.skippedIds ?? []);
  const snapshots = new Map(original.messages.map((m) => [m.id, m]));

  if (leavesFolder(original.action)) {
    const landed = original.action === "move" ? original.targetFolderId : original.landedFolderId;
    if (!landed) return [];
    const sources = (
      original.sources?.length
        ? original.sources
        : original.messages.flatMap((m) => (m.folderId ? [{ id: m.id, folderId: m.folderId }] : []))
    ).filter((s) => !skipped.has(s.id) && s.folderId !== landed);
    const byFolder = new Map<string, IntentMessage[]>();
    const unread: IntentMessage[] = [];
    for (const source of sources) {
      const snapshot = snapshots.get(source.id);
      const message: IntentMessage = {
        id: source.id, folderId: landed,
        isSeen: snapshot?.isSeen ?? true, isFlagged: snapshot?.isFlagged ?? false,
        threadId: snapshot?.threadId ?? null, row: snapshot?.row,
      };
      const group = byFolder.get(source.folderId) ?? [];
      group.push(message);
      byFolder.set(source.folderId, group);
      if (snapshot && !snapshot.isSeen) {
        unread.push({ ...message, folderId: source.folderId, row: undefined });
      }
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
  const messages = original.messages
    .filter((m) => !skipped.has(m.id) && pair[1](m))
    .map(({ row: _row, ...m }) => m);
  if (messages.length === 0) return [];
  return [{ ...base, id: newId(), action: pair[0], messages }];
}

/** Undo steps still worth offering: young enough, newest last, bounded. */
export function pruneUndo(entries: readonly UndoEntry[], now: number): UndoEntry[] {
  const young = entries.filter((e) => now - e.createdAt <= UNDO_MAX_AGE_MS);
  return young.slice(Math.max(0, young.length - UNDO_STACK_LIMIT));
}

// --- Two tabs --------------------------------------------------------------

/** A settled intent never goes back to unsettled within a generation; a
 * held one outranks the pending it was. Retry and Send start a new
 * generation, which outranks every copy of the old one. */
const STATE_RANK: Record<IntentState, number> = {
  pending: 0, inflight: 0, held: 1, done: 2, failed: 2,
};

/**
 * Two copies of the ledger, one per tab, merged: every intent either holds
 * that has not been retired, the more advanced copy of each winning.
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
    if (!held || outranks(intent, held)) merged.set(intent.id, intent);
  }
  return [...merged.values()].sort(byCreation);
}

/**
 * Two tabs' copies of one undo step: each intent's more advanced copy, which
 * the tab that sends keeps current, with whichever copy's row snapshots
 * survived storage.
 */
export function mergeUndoEntry(ours: UndoEntry, theirs: UndoEntry): UndoEntry {
  const other = new Map(theirs.intents.map((i) => [i.id, i]));
  const intents = ours.intents.map((mine) => {
    const copy = other.get(mine.id);
    if (!copy) return mine;
    const winner = outranks(copy, mine) ? copy : mine;
    const withRows = mine.messages.some((m) => m.row) ? mine : copy;
    return { ...winner, messages: withRows.messages };
  });
  return { ...ours, intents };
}

function outranks(a: MailIntent, b: MailIntent): boolean {
  const generation = (a.generation ?? 0) - (b.generation ?? 0);
  if (generation !== 0) return generation > 0;
  const rank = STATE_RANK[a.state] - STATE_RANK[b.state];
  if (rank !== 0) return rank > 0;
  return a.updatedAt > b.updatedAt;
}

/** Drop anything in a stored ledger that is not the shape this code reads,
 * rather than failing on it at render. */
export function validIntents(value: unknown): MailIntent[] {
  if (!Array.isArray(value)) return [];
  return value.filter((i): i is MailIntent => {
    if (!i || typeof i !== "object") return false;
    const intent = i as Partial<MailIntent>;
    return (
      typeof intent.id === "string" &&
      typeof intent.accountId === "string" &&
      typeof intent.action === "string" && intent.action in ACTION_LABELS &&
      typeof intent.state === "string" && intent.state in STATE_RANK &&
      typeof intent.createdAt === "number" &&
      typeof intent.updatedAt === "number" &&
      Array.isArray(intent.messages) &&
      intent.messages.every((m) => m && typeof m === "object" && typeof m.id === "string")
    );
  }).map((i) => ({ ...i, generation: i.generation ?? 0, attempts: i.attempts ?? 0 }));
}
