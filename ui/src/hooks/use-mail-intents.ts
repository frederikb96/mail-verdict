/**
 * Taking mail actions: turning a click or a key into intents, moving the
 * reading pane on, offering undo, and reacting to what the drainer reports.
 *
 * Every surface that acts on mail -- a row's controls, the reading pane's
 * toolbar, keyboard shortcuts, drag and drop, the bulk panel -- goes through
 * useMailAction, so the next message opened, the undo step and the toast
 * are decided once.
 */

import { useCallback, useEffect } from "react";
import { api } from "@/lib/api";
import { mailKeys } from "@/hooks/use-mails";
import { type InfiniteData, type Query, type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { refreshMailViews, refreshThreads } from "@/hooks/use-mails";
import { useToast } from "@/hooks/use-toast";
import { activeReplyDirtyForThreadIdAtom, explicitlyUnreadMailIdAtom, selectedMailIdAtom } from "@/lib/atoms";
import { newIdempotencyKey } from "@/lib/idempotency-key";
import { kickDrainer, retryIntent, startDrainer } from "@/lib/intent-drainer";
import {
  addIntents,
  getLedgerSnapshot,
  retireIntents,
  takeUndo,
  updateIntent,
} from "@/lib/intent-ledger";
import { markKeptWhileUnread } from "@/lib/mail-list-window";
import {
  ACTION_LABELS,
  ROW_SNAPSHOT_LIMIT,
  isUndoable,
  leavesFolder,
  projectRows,
  projectThreadMessages,
  reversalsOf,
  type IntentAction,
  type IntentMessage,
  type MailIntent,
} from "@/lib/mail-intents";
import { isMailListQuery } from "@/lib/query-persister";
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

interface CachedList {
  query: Query;
  rows: MessageSummary[];
}

/** Every list the client holds rows for: folders, unified views and the
 * quick filter's results. Lists someone is looking at come first. */
function cachedLists(qc: QueryClient): CachedList[] {
  const lists = qc.getQueryCache().findAll({
    predicate: (q) => isMailListQuery(q.queryKey) || q.queryKey[0] === "search",
  });
  return lists
    .map((query) => {
      const data = query.state.data as InfiniteData<{ messages?: MessageSummary[]; items?: MessageSummary[] }> | undefined;
      const rows = data?.pages?.flatMap((page) => page.messages ?? page.items ?? []) ?? [];
      return { query, rows };
    })
    .sort((a, b) => b.query.getObserversCount() - a.query.getObserversCount());
}

/** A message as the person last saw it -- its row with every intent already
 * taken on it applied -- for the counts and the undo a new intent needs. */
function snapshotMessage(qc: QueryClient, id: string, keepRow: boolean): IntentMessage {
  const { intents } = getLedgerSnapshot();
  for (const { query, rows } of cachedLists(qc)) {
    const row = rows.find((r) => r.id === id);
    if (!row) continue;
    const [seen] = projectThreadMessages([row], intents, query.state.dataUpdatedAt);
    return {
      id, folderId: seen.folder_id, isSeen: seen.is_seen, isFlagged: seen.is_flagged,
      threadId: row.thread_id, row: keepRow ? row : undefined,
    };
  }
  for (const [, thread] of qc.getQueriesData<ThreadResponse>({ queryKey: ["thread"] })) {
    const message = thread?.messages.find((m) => m.id === id);
    if (!message) continue;
    const [seen] = projectThreadMessages([message], intents, 0);
    return {
      id, folderId: seen.folder_id, isSeen: seen.is_seen, isFlagged: seen.is_flagged,
      threadId: message.thread_id,
    };
  }
  return { id, folderId: null, isSeen: true, isFlagged: false, threadId: null };
}

/**
 * The message that should take the reader's place when `mailId` leaves the
 * list: its neighbour in `direction`, or the one on the other side when
 * there is nothing that way. Read from the lists as they are shown, so a
 * message another pending action already took away is never chosen.
 */
function neighbourOf(qc: QueryClient, mailId: string, direction: MailNavDirection): string | null {
  const { intents } = getLedgerSnapshot();
  for (const { query, rows } of cachedLists(qc)) {
    if (!rows.some((r) => r.id === mailId)) continue;
    const shown = projectRows(rows, intents, {
      dataUpdatedAt: query.state.dataUpdatedAt, scopeFolderIds: null, threaded: false, hasMore: true,
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
}

export interface PerformOptions {
  /** Whether Ctrl+Z and a toast may take this back. Off for what the
   * application does on its own, such as marking an opened message read. */
  undoable?: boolean;
}

/** Take an undo step back: the newest, or the one named. Returns its label. */
export function undoMailAction(entryId?: string): string | null {
  const entry = takeUndo(entryId);
  if (!entry) return null;
  const now = Date.now();
  const { intents } = getLedgerSnapshot();
  const retire: string[] = [];
  const reversals: MailIntent[] = [];
  for (const copy of entry.intents) {
    const live = intents.find((i) => i.id === copy.id);
    if (live && ((live.state === "pending" && live.attempts === 0) || live.state === "failed")) {
      // Never sent, or refused: nothing reached the server to reverse.
      retire.push(live.id);
      continue;
    }
    if (live && (live.state === "inflight" || live.state === "pending")) {
      // Out, or retrying one that may have landed with its answer lost --
      // reversed once the server has answered it (intent-drainer.ts).
      updateIntent(live.id, { undoRequested: true });
      continue;
    }
    if (live) retire.push(live.id);
    reversals.push(...reversalsOf({ ...copy, sources: live?.sources ?? copy.sources }, now, newIdempotencyKey));
  }
  retireIntents(retire);
  if (reversals.length > 0) addIntents(reversals);
  kickDrainer();
  return entry.label;
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

      let snapshots = 0;
      const intents: MailIntent[] = nonEmpty.map((input) => ({
        id: newIdempotencyKey(),
        accountId: input.accountId,
        action: input.action,
        targetFolderId: input.targetFolderId,
        messages: input.mailIds.map((id) => snapshotMessage(qc, id, snapshots++ < ROW_SNAPSHOT_LIMIT)),
        bulk: input.bulk ?? input.mailIds.length > 1,
        expandThreads: input.expandThreads,
        createdAt: now,
        state: "pending",
        attempts: 0,
        notBefore: now,
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

/** Ctrl+Z / Cmd+Z's own action: undo the newest step and say so. */
export function useUndoMailAction() {
  const { push: pushToast } = useToast();
  return useCallback((): boolean => {
    const label = undoMailAction();
    if (label === null) return false;
    pushToast(`Undone: ${label}`, "info", 3000);
    return true;
  }, [pushToast]);
}

/** Start sending intents and react to what comes back. Mounted once. */
export function useMailIntentDrainer(): void {
  const qc = useQueryClient();
  const { push: pushToast, dismiss } = useToast();

  useEffect(() => {
    startDrainer({
      onSettled: (intent) => {
        undoToastByIntent.delete(intent.id);
        const ids = new Set(intent.messages.map((m) => m.id));
        // A count fetch that began before the write must not land after
        // it and look current.
        void qc.cancelQueries({ queryKey: ["folders"] });
        void qc.cancelQueries({ queryKey: ["folder-order"] });
        void qc.cancelQueries({ queryKey: ["unified", "folders"] });
        for (const id of ids) qc.invalidateQueries({ queryKey: ["mail", id] });
        refreshThreads(qc, ids, false);
        const folders = [...intent.messages.map((m) => m.folderId), intent.targetFolderId];
        const known = folders.filter((id): id is string => !!id);
        // An archive, trash or spam lands in a folder the client does not
        // know -- the event for its arrival there re-reads that folder's lists.
        const unknownOrigin = intent.messages.some((m) => m.folderId === null);
        refreshMailViews(qc, unknownOrigin ? null : { folderIds: new Set(known) });
      },
      onFailed: (intent) => {
        const toastId = undoToastByIntent.get(intent.id);
        if (toastId) dismiss(toastId);
        undoToastByIntent.delete(intent.id);
        const verb = intent.reverses ? "undo" : ACTION_LABELS[intent.action];
        pushToast(`Could not ${verb}: ${intent.lastError ?? ""}`, "error", 0, {
          label: "Retry",
          onClick: () => retryIntent(intent.id),
        }, {
          label: "Discard",
          onClick: () => retireIntents([intent.id]),
        });
      },
      onNothingToUndo: () => {
        pushToast("Nothing to undo — the message has moved since", "info", 5000);
      },
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
  const snapshot = snapshotMessage(qc, mailId, false);
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
      const { intents } = getLedgerSnapshot();
      const shown = projectThreadMessages(thread.messages, intents, Date.now());
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
