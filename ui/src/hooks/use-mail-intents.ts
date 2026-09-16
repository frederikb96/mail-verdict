/**
 * Taking mail actions: turning a click or a key into intents, moving the
 * reading pane on, offering undo, and telling the person what came of it.
 *
 * Every surface that acts on mail -- a row's controls, the reading pane's
 * toolbar and verdict thumbs, keyboard shortcuts, drag and drop, the bulk
 * panel, spam review -- goes through useMailAction, so the next message
 * opened, the undo step and the toast are decided once. Folder-wide actions
 * over a predicate are the exception: nothing client-side knows which
 * messages they cover (use-selection.ts).
 */

import { useCallback, useEffect } from "react";
import { type InfiniteData, type Query, type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { api } from "@/lib/api";
import { mailKeys, refreshMailViews, refreshThreads } from "@/hooks/use-mails";
import { useToast } from "@/hooks/use-toast";
import { activeReplyDirtyForThreadIdAtom, explicitlyUnreadMailIdAtom, selectedMailIdAtom } from "@/lib/atoms";
import { newIdempotencyKey } from "@/lib/idempotency-key";
import { kickDrainer, startDrainer } from "@/lib/intent-drainer";
import {
  addIntents,
  currentTabId,
  getLedgerSnapshot,
  projectableIntents,
  requestUndo,
  restartIntent,
  retireIntents,
  subscribeLedger,
  takeUndo,
} from "@/lib/intent-ledger";
import { markKeptWhileUnread } from "@/lib/mail-list-window";
import {
  ACTION_LABELS,
  ROW_SNAPSHOT_LIMIT,
  isUndoable,
  leavesFolder,
  projectRows,
  projectThreadMessages,
  type IntentAction,
  type IntentMessage,
  type MailIntent,
} from "@/lib/mail-intents";
import { isMailListQuery } from "@/lib/query-persister";
import { readTimeOf } from "@/lib/read-clock";
import { type MailNavDirection, mailNavDirectionAtom } from "@/store/mail-nav-atom";
import type { MessageSummary, ThreadResponse } from "@/types/api";

/** What a single message's action says once taken, as in "Archived". */
const DONE_LABELS: Record<IntentAction, string> = {
  mark_read: "Marked as read",
  mark_unread: "Marked as unread",
  flag: "Starred",
  unflag: "Unstarred",
  move: "Moved",
  archive: "Archived",
  trash: "Moved to trash",
  expunge: "Deleted forever",
  spam: "Marked as spam",
  not_spam: "Marked as not spam",
};

/** The same for a selection, after its count, as in "3 messages archived". */
const BULK_DONE_LABELS: Record<IntentAction, string> = {
  mark_read: "marked as read",
  mark_unread: "marked as unread",
  flag: "starred",
  unflag: "unstarred",
  move: "moved",
  archive: "archived",
  trash: "moved to trash",
  expunge: "deleted forever",
  spam: "marked as spam",
  not_spam: "marked as not spam",
};

/** Actions whose undo is offered in a toast the moment they are taken --
 * the ones that take a message out of sight. Every undoable action can
 * also be taken back with Ctrl+Z. */
const TOASTED_ACTIONS: ReadonlySet<IntentAction> = new Set(["archive", "trash", "spam"]);

/** The toast offering undo for an intent, closed if the intent fails. */
const undoToastByIntent = new Map<string, string>();

interface CachedRow {
  row: MessageSummary;
  readAt: number;
}

/** Every row the client holds, freshest copy of each: lists, the quick
 * filter's results and conversations. */
function cachedRows(qc: QueryClient): Map<string, CachedRow> {
  const rows = new Map<string, CachedRow>();
  const consider = (row: MessageSummary, readAt: number) => {
    const held = rows.get(row.id);
    if (!held || held.readAt < readAt) rows.set(row.id, { row, readAt });
  };
  for (const query of qc.getQueryCache().getAll()) {
    const key = query.queryKey;
    const readAt = readTimeOf(key, query.state.dataUpdatedAt);
    if (isMailListQuery(key) || key[0] === "search") {
      const data = query.state.data as
        | InfiniteData<{ messages?: MessageSummary[]; items?: MessageSummary[] }>
        | undefined;
      for (const page of data?.pages ?? []) {
        for (const row of page.messages ?? page.items ?? []) consider(row, readAt);
      }
    } else if (key[0] === "thread") {
      for (const message of (query.state.data as ThreadResponse | undefined)?.messages ?? []) {
        consider(message, readAt);
      }
    }
  }
  return rows;
}

/** A message as the person last saw it -- its freshest cached row with
 * every intent already taken on it applied. */
function snapshotMessage(
  cached: ReadonlyMap<string, CachedRow>, intents: readonly MailIntent[], id: string,
  keepRow: boolean, seenIn?: string,
): IntentMessage {
  const found = cached.get(id);
  if (!found) return { id, folderId: seenIn ?? null, isSeen: true, isFlagged: false, threadId: null };
  const [seen] = projectThreadMessages([found.row], intents, found.readAt);
  return {
    id, folderId: seen.folder_id, isSeen: seen.is_seen, isFlagged: seen.is_flagged,
    threadId: found.row.thread_id, mirroredAt: found.row.mirrored_at ?? null,
    row: keepRow ? found.row : undefined,
  };
}

/**
 * The message that should take the reader's place when `mailId` leaves the
 * list: its neighbour in `direction`, or the one on the other side when
 * there is nothing that way. Read from the list someone is looking at, as
 * shown, so a message another pending action already took away is never
 * chosen.
 */
function neighbourOf(qc: QueryClient, mailId: string, direction: MailNavDirection): string | null {
  const intents = projectableIntents(getLedgerSnapshot());
  const lists = qc
    .getQueryCache()
    .findAll({ predicate: (q) => isMailListQuery(q.queryKey) || q.queryKey[0] === "search" })
    .sort((a, b) => b.getObserversCount() - a.getObserversCount());
  for (const query of lists) {
    const data = query.state.data as
      | InfiniteData<{ messages?: MessageSummary[]; items?: MessageSummary[] }>
      | undefined;
    const rows = data?.pages?.flatMap((page) => page.messages ?? page.items ?? []) ?? [];
    if (!rows.some((r) => r.id === mailId)) continue;
    const shown = projectRows(rows, intents, {
      readAt: readTimeOf(query.queryKey, query.state.dataUpdatedAt),
      scopeFolderIds: null, threaded: false, hasMore: true,
    });
    const ids = shown.map((r) => r.id);
    const at = ids.indexOf(mailId);
    if (at < 0) continue;
    const step = direction === "older" ? 1 : -1;
    return ids[at + step] ?? ids[at - step] ?? null;
  }
  return null;
}

export interface MailActionInput {
  accountId: string;
  mailIds: string[];
  action: IntentAction;
  targetFolderId?: string;
  /** Sent through the bulk endpoint even for one message. */
  bulk?: boolean;
  /** Each id is a conversation row standing for its whole conversation. */
  expandThreads?: boolean;
  /** Where the caller showed each message, for one no cached list holds. */
  seenFolderIds?: Record<string, string>;
}

export interface PerformOptions {
  /** Whether Ctrl+Z and a toast may take this back. Off for what the
   * application does on its own, such as marking an opened message read. */
  undoable?: boolean;
}

/**
 * Undo a step: the one named, or the newest this tab took. The tab that
 * sends carries it out once every intent in it has an answer
 * (intent-drainer.ts); until then the step stops showing at once. Returns
 * the step's label.
 */
export function undoMailAction(entryId?: string): string | null {
  const entry = takeUndo(entryId, entryId ? undefined : currentTabId());
  if (!entry) return null;
  requestUndo(entry);
  kickDrainer();
  return entry.label;
}

/** Send a refused intent again. */
export function retryIntent(id: string): void {
  restartIntent(id, { resend: false });
  kickDrainer();
}

/** Send an intent held for being old, now the person has confirmed it. */
export function sendHeldIntent(id: string): void {
  restartIntent(id, { resend: true });
  kickDrainer();
}

/** Give up on a refused or held intent. */
export function discardIntent(id: string): void {
  retireIntents([id]);
}

/** Record the intents one user action makes, and send them. */
export function useMailAction() {
  const qc = useQueryClient();
  // Selected mail lives in the same store every action initiator (list row,
  // reading pane, bulk toolbar) reads from, so moving it on here reaches all
  // of them: once the open message leaves its folder, nothing keeps acting
  // on it under a reading pane that still shows its old content -- except a
  // reply or forward in progress against its thread, which unmounting the
  // pane would take down too.
  //
  // This writes selectedMailIdAtom directly rather than through
  // requestSelectMailAtom (lib/atoms.ts): that atom answers "is some
  // composer dirty at all", which is the wrong question here -- an action
  // taken elsewhere on a message must still go through even while a reply
  // on some unrelated thread sits open.
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const activeReplyDirtyForThreadId = useAtomValue(activeReplyDirtyForThreadIdAtom);
  const setExplicitlyUnread = useSetAtom(explicitlyUnreadMailIdAtom);
  const navDirection = useAtomValue(mailNavDirectionAtom);
  const { push: pushToast } = useToast();

  /** Several inputs are one user action -- one undo step, one toast. */
  const performAll = useCallback(
    (inputs: MailActionInput[], { undoable = true }: PerformOptions = {}) => {
      const nonEmpty = inputs.filter((input) => input.mailIds.length > 0);
      if (nonEmpty.length === 0) return;
      const { action } = nonEmpty[0];
      const allIds = nonEmpty.flatMap((input) => input.mailIds);
      const now = Date.now();
      const tab = currentTabId();

      // Recorded before the intent, so the reading pane's auto-read effect
      // sees it in the same render as the unread flip that effect reacts to.
      for (const id of allIds) {
        if (action === "mark_unread") setExplicitlyUnread(id);
        if (action === "mark_read") {
          setExplicitlyUnread((cur) => (cur === id ? null : cur));
          // An unread-only window keeps showing this row until the reader
          // navigates away -- see keptWhileUnreadIds' own doc comment.
          markKeptWhileUnread(id);
        }
      }

      const cached = cachedRows(qc);
      const current = projectableIntents(getLedgerSnapshot());
      let snapshots = 0;
      const intents: MailIntent[] = nonEmpty.map((input) => ({
        id: newIdempotencyKey(),
        accountId: input.accountId,
        action: input.action,
        targetFolderId: input.targetFolderId,
        messages: input.mailIds.map((id) =>
          snapshotMessage(
            cached, current, id, snapshots++ < ROW_SNAPSHOT_LIMIT, input.seenFolderIds?.[id],
          )),
        bulk: input.bulk ?? input.mailIds.length > 1,
        expandThreads: input.expandThreads,
        createdAt: now,
        state: "pending",
        attempts: 0,
        notBefore: now,
        generation: 0,
        originTab: tab,
        updatedAt: now,
      }));

      if (leavesFolder(action) && selectedMailId && allIds.includes(selectedMailId)) {
        // A reply or forward in progress against this message's thread
        // must not be discarded by unmounting the reading pane under it.
        // Matched on the thread: the pane's open message may be an older
        // one of the conversation the reply is not addressed to.
        const open = intents.flatMap((i) => i.messages).find((m) => m.id === selectedMailId);
        const hasDirtyReply = open?.threadId != null && open.threadId === activeReplyDirtyForThreadId;
        if (!hasDirtyReply) {
          // Read before the intent is recorded, off the list as shown.
          setSelectedMailId(allIds.length === 1 ? neighbourOf(qc, selectedMailId, navDirection) : null);
        }
      }

      const label =
        allIds.length === 1 ? DONE_LABELS[action] : `${allIds.length} messages ${BULK_DONE_LABELS[action]}`;
      const entry = addIntents(intents, undoable && isUndoable(action) ? label : undefined);
      kickDrainer();

      if (entry && TOASTED_ACTIONS.has(action)) {
        const toastId = pushToast(label, "success", 6000, {
          label: "Undo",
          onClick: () => undoMailAction(entry.id),
        });
        for (const intent of intents) undoToastByIntent.set(intent.id, toastId);
      }
    },
    [qc, selectedMailId, activeReplyDirtyForThreadId, setExplicitlyUnread, setSelectedMailId, navDirection, pushToast],
  );

  const perform = useCallback(
    (input: MailActionInput, options?: PerformOptions) => performAll([input], options),
    [performAll],
  );

  return { perform, performAll };
}

/** Ctrl+Z / Cmd+Z's own action: undo this tab's newest step and say so. */
export function useUndoMailAction() {
  const { push: pushToast } = useToast();
  return useCallback((): boolean => {
    const label = undoMailAction();
    if (label === null) return false;
    pushToast(`Undone: ${label}`, "info", 3000);
    return true;
  }, [pushToast]);
}

/** Past tense of an action, for an outcome, as in "Not archived". */
const PAST: Record<IntentAction, string> = {
  mark_read: "marked as read", mark_unread: "marked as unread", flag: "starred",
  unflag: "unstarred", move: "moved", archive: "archived", trash: "moved to trash",
  expunge: "deleted", spam: "marked as spam", not_spam: "marked as not spam",
};

/**
 * Drop every cached list or conversation nobody is looking at that still
 * shows messages as they were before `intent` was answered. Refreshing
 * only re-reads what is on screen, and such a cache outlives the intent
 * that hides its rows -- opened later, it would show the change undone
 * until its own re-read landed. Removed, it simply loads afresh.
 */
function dropStaleUnobserved(qc: QueryClient, intent: MailIntent): void {
  const ids = new Set([
    ...intent.messages.map((m) => m.id), ...(intent.skippedIds ?? []),
    ...(intent.sources ?? []).map((s) => s.id),
  ]);
  if (ids.size === 0 || intent.doneAt === undefined) return;
  const stale: Query[] = [];
  for (const query of qc.getQueryCache().getAll()) {
    if (query.getObserversCount() > 0) continue;
    const key = query.queryKey;
    if (readTimeOf(key, query.state.dataUpdatedAt) >= intent.doneAt) continue;
    let holds = false;
    if (isMailListQuery(key) || key[0] === "search") {
      const data = query.state.data as
        | InfiniteData<{ messages?: MessageSummary[]; items?: MessageSummary[] }>
        | undefined;
      holds = !!data?.pages?.some((p) => (p.messages ?? p.items ?? []).some((r) => ids.has(r.id)));
    } else if (key[0] === "thread") {
      holds = !!(query.state.data as ThreadResponse | undefined)?.messages.some((m) => ids.has(m.id));
    }
    if (holds) stale.push(query);
  }
  for (const query of stale) qc.removeQueries({ queryKey: query.queryKey, exact: true });
}

/**
 * Start the drainer, and act on what happens to intents in every tab: once
 * one is answered, re-read what it touched here; tell this tab's person
 * about the outcome of what they did here. Mounted once per page.
 */
export function useMailIntentOutcomes(): void {
  const qc = useQueryClient();
  const { push: pushToast, dismiss } = useToast();

  useEffect(() => {
    startDrainer();
    const me = currentTabId();
    const seen = new Map<string, string>();
    const keyOf = (i: MailIntent) => `${i.generation ?? 0}:${i.state}`;
    for (const intent of getLedgerSnapshot().intents) seen.set(intent.id, keyOf(intent));

    const onDone = (intent: MailIntent) => {
      undoToastByIntent.delete(intent.id);
      const ids = new Set([...intent.messages.map((m) => m.id), ...(intent.skippedIds ?? [])]);
      // A count fetch that began before the write must not land after it
      // and look current.
      void qc.cancelQueries({ queryKey: ["folders"] });
      void qc.cancelQueries({ queryKey: ["folder-order"] });
      void qc.cancelQueries({ queryKey: ["unified", "folders"] });
      for (const id of ids) qc.invalidateQueries({ queryKey: ["mail", id] });
      refreshThreads(qc, ids, false);
      dropStaleUnobserved(qc, intent);
      const folders = [
        ...intent.messages.map((m) => m.folderId), intent.targetFolderId, intent.landedFolderId,
      ];
      const unknownOrigin = intent.messages.some((m) => m.folderId === null);
      refreshMailViews(
        qc,
        unknownOrigin ? null : { folderIds: new Set(folders.filter((id): id is string => !!id)) },
      );
      if (intent.originTab !== me) return;
      // A reversal speaks through its move alone: the mark-unread following
      // it misses exactly when the move did.
      if (intent.reverses !== undefined && intent.action !== "move") return;
      if (intent.notApplied) {
        pushToast(
          intent.reverses
            ? "Nothing to undo — the message has moved since"
            : `Not ${PAST[intent.action]} — the message had already moved`,
          "info", 6000,
        );
      } else if (intent.skippedIds?.length) {
        const count = intent.skippedIds.length;
        pushToast(
          `${count} message${count === 1 ? " was" : "s were"} left alone — ${count === 1 ? "it" : "they"} had moved since`,
          "info", 6000,
        );
      }
    };

    const onFailed = (intent: MailIntent) => {
      refreshMailViews(qc, null);
      if (intent.originTab !== me) return;
      const toastId = undoToastByIntent.get(intent.id);
      if (toastId) dismiss(toastId);
      undoToastByIntent.delete(intent.id);
      const verb = intent.reverses ? "undo" : ACTION_LABELS[intent.action];
      pushToast(
        `Could not ${verb}: ${intent.lastError ?? ""}`, "error", 0,
        { label: "Retry", onClick: () => retryIntent(intent.id) },
        { label: "Discard", onClick: () => discardIntent(intent.id) },
      );
    };

    return subscribeLedger(() => {
      for (const intent of getLedgerSnapshot().intents) {
        const key = keyOf(intent);
        if (seen.get(intent.id) === key) continue;
        seen.set(intent.id, key);
        if (intent.state === "done") onDone(intent);
        else if (intent.state === "failed") onFailed(intent);
      }
    });
  }, [qc, pushToast, dismiss]);
}

/** The open message's folder and thread, read first from the conversation
 * the reading pane renders from -- always cached while the message is on
 * screen -- then from the loaded lists. */
export function openMailInCache(
  qc: QueryClient, mailId: string,
): { folderId: string | null; threadId: string | null } | null {
  const own = qc
    .getQueryData<ThreadResponse>(mailKeys.thread(mailId))
    ?.messages.find((m) => m.id === mailId);
  if (own) return { folderId: own.folder_id, threadId: own.thread_id };
  const snapshot = snapshotMessage(cachedRows(qc), [], mailId, false);
  return snapshot.threadId === null ? null : snapshot;
}

/**
 * Mark read every unread message of a conversation row's thread in that
 * row's folder -- or, for a unified view's row, in any of the view's folders
 * (`folderIds`) -- what reading a row grouped by conversation means, since
 * the row counts all of them (isRowUnread). The row's own message is left
 * out unless `includeRow`: opening a row already marks it read through the
 * reading pane. Taken back by Ctrl+Z only when the reader asked for it
 * (`includeRow`), not when opening the row did it.
 */
export function useMarkConversationRead() {
  const qc = useQueryClient();
  const { push: pushToast } = useToast();
  const { perform } = useMailAction();
  return useCallback(
    async (row: MessageSummary, includeRow: boolean, folderIds?: readonly string[]) => {
      let thread: ThreadResponse;
      const startedAt = Date.now();
      try {
        thread = await qc.fetchQuery({
          queryKey: mailKeys.thread(row.id),
          queryFn: () => api.mails.thread(row.id),
          staleTime: 0,
        });
      } catch (err) {
        pushToast(`Could not mark as read: ${(err as Error).message}`, "error", 0);
        return;
      }
      const inScope = (folderId: string) =>
        folderIds ? folderIds.includes(folderId) : folderId === row.folder_id;
      const intents = projectableIntents(getLedgerSnapshot());
      const shown = projectThreadMessages(thread.messages, intents, startedAt);
      const ids = shown
        .filter((m) => inScope(m.folder_id) && !m.is_seen)
        .filter((m) => includeRow || m.id !== row.id)
        .map((m) => m.id);
      perform(
        { accountId: row.account_id, mailIds: ids, action: "mark_read", bulk: true },
        { undoable: includeRow },
      );
    },
    [qc, pushToast, perform],
  );
}
