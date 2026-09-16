import { test } from "node:test";
import assert from "node:assert/strict";
import {
  classifyFailure,
  folderCountDeltas,
  mergeIntents,
  nextWakeAt,
  projectCounts,
  projectRows,
  projectThreadMessages,
  pruneUndo,
  retryDelay,
  reversalsOf,
  sendableIntents,
  UNDO_MAX_AGE_MS,
  UNDO_STACK_LIMIT,
  type IntentAction,
  type ListProjection,
  type MailIntent,
  type UndoEntry,
} from "./mail-intents.ts";
import type { MessageSummary } from "@/types/api";

const INBOX = "f-inbox";
const WORK = "f-work";
const ARCHIVE = "f-archive";

function row(id: string, minutesAgo: number, extra: Partial<MessageSummary> = {}): MessageSummary {
  return {
    id, account_id: "acct", folder_id: INBOX, thread_id: `t-${id}`, subject: id,
    from_addr: null, to_addrs: null,
    received_at: new Date(Date.UTC(2026, 8, 16, 12, 0) - minutesAgo * 60_000).toISOString(),
    is_seen: false, is_flagged: false, is_answered: false, is_draft: false, snippet: null,
    pending_sync: false, is_truncated: false, has_attachments: false, verdict_is_spam: null,
    ...extra,
  };
}

let nextId = 0;
function intent(
  action: IntentAction,
  rows: MessageSummary[],
  extra: Partial<MailIntent> = {},
): MailIntent {
  nextId += 1;
  const createdAt = extra.createdAt ?? 1000 + nextId;
  return {
    id: `i${nextId}`, accountId: "acct", action, bulk: rows.length > 1, createdAt,
    state: "pending", attempts: 0, notBefore: createdAt, updatedAt: createdAt,
    messages: rows.map((r) => ({
      id: r.id, folderId: r.folder_id, isSeen: r.is_seen, isFlagged: r.is_flagged,
      threadId: r.thread_id, row: r,
    })),
    ...extra,
  };
}

const folderList = (extra: Partial<ListProjection> = {}): ListProjection => ({
  dataUpdatedAt: 5000, scopeFolderIds: new Set([INBOX]), threaded: false, hasMore: false, ...extra,
});
const ids = (rows: { id: string }[]) => rows.map((r) => r.id);

test("a pending archive hides its row however recently the list was read", () => {
  const rows = [row("a", 1), row("b", 2), row("c", 3)];
  const archive = intent("archive", [rows[1]], { createdAt: 100 });
  const projected = projectRows(rows, [archive], folderList({ dataUpdatedAt: 999_999 }));
  assert.deepEqual(ids(projected), ["a", "c"]);
});

test("the same row leaves a unified view over several folders", () => {
  const rows = [row("a", 1), row("w", 2, { folder_id: WORK })];
  const archive = intent("archive", [rows[1]]);
  const projected = projectRows(
    rows, [archive], folderList({ scopeFolderIds: new Set([INBOX, WORK]) }),
  );
  assert.deepEqual(ids(projected), ["a"]);
});

test("a done intent leaves lists read after it alone, and still hides from older ones", () => {
  const rows = [row("a", 1), row("b", 2)];
  const archive = intent("archive", [rows[1]], { state: "done", doneAt: 5000 });
  assert.deepEqual(ids(projectRows(rows, [archive], folderList({ dataUpdatedAt: 4000 }))), ["a"]);
  const fresh = projectRows(rows, [archive], folderList({ dataUpdatedAt: 6000 }));
  assert.equal(fresh, rows, "the server read after the write is shown as it is");
});

test("a row the server kept in place reappears once a read after the action lands", () => {
  // Marking spam a message already in Junk leaves it there.
  const rows = [row("j", 1, { folder_id: "f-junk" })];
  const spam = intent("spam", rows, { state: "done", doneAt: 5000 });
  const list = folderList({ scopeFolderIds: new Set(["f-junk"]) });
  assert.deepEqual(ids(projectRows(rows, [spam], { ...list, dataUpdatedAt: 4000 })), []);
  assert.deepEqual(ids(projectRows(rows, [spam], { ...list, dataUpdatedAt: 6000 })), ["j"]);
});

test("a move hides the row everywhere but its target folder", () => {
  const moved = row("m", 1);
  const move = intent("move", [moved], { targetFolderId: WORK });
  assert.deepEqual(ids(projectRows([moved], [move], folderList())), []);
  const inTarget = { ...moved, folder_id: WORK };
  assert.deepEqual(
    ids(projectRows([inTarget], [move], folderList({ scopeFolderIds: new Set([WORK]) }))), ["m"],
  );
});

test("flag and read intents override the row and mark it pending", () => {
  const rows = [row("a", 1)];
  const read = intent("mark_read", rows, { createdAt: 10 });
  const star = intent("flag", rows, { createdAt: 11 });
  const [projected] = projectRows(rows, [read, star], folderList());
  assert.equal(projected.is_seen, true);
  assert.equal(projected.is_flagged, true);
  assert.equal(projected.pendingSince, 10);
});

test("later intents on the same message win", () => {
  const rows = [row("a", 1)];
  const read = intent("mark_read", rows, { createdAt: 10 });
  const unread = intent("mark_unread", rows, { createdAt: 20 });
  assert.equal(projectRows(rows, [unread, read], folderList())[0].is_seen, false);
});

test("a failed intent changes nothing but marks its row", () => {
  const rows = [row("a", 1)];
  const archive = intent("archive", rows, { state: "failed", lastError: "No archive folder" });
  const [projected] = projectRows(rows, [archive], folderList());
  assert.equal(projected.id, "a");
  assert.deepEqual(projected.failedIntent, {
    id: archive.id, action: "archive", error: "No archive folder",
  });
});

test("an undone intent stops projecting at once", () => {
  const rows = [row("a", 1)];
  const archive = intent("archive", rows, { state: "inflight", undoRequested: true });
  assert.equal(projectRows(rows, [archive], folderList()), rows);
});

test("no intent touching the rows returns the rows themselves", () => {
  const rows = [row("a", 1)];
  const other = intent("archive", [row("z", 9)]);
  assert.equal(projectRows(rows, [other], folderList()), rows);
});

test("a conversation-expanded archive hides the conversation's other row too", () => {
  const newest = row("n", 1, { thread_id: "t1" });
  const older = row("o", 5, { thread_id: "t1" });
  const archive = intent("archive", [newest], { expandThreads: true });
  // After a partial refresh the conversation's row can be another message.
  assert.deepEqual(ids(projectRows([older], [archive], folderList({ threaded: true }))), []);
});

test("moving a message back shows its row again before any list is re-read", () => {
  const rows = [row("a", 1), row("c", 3)];
  const b = row("b", 2);
  const archive = intent("archive", [b], { state: "done", doneAt: 4000 });
  const [back] = reversalsOf(archive, 7000, () => "r1");
  const projected = projectRows(rows, [archive, back], folderList({ dataUpdatedAt: 6000 }));
  assert.deepEqual(ids(projected), ["a", "b", "c"]);
  assert.equal(projected[1].folder_id, INBOX);
});

test("a restored row below a window with more to load is left for paging", () => {
  const rows = [row("a", 1)];
  const old = row("old", 500);
  const archive = intent("archive", [old], { state: "done", doneAt: 4000 });
  const [back] = reversalsOf(archive, 7000, () => "r1");
  const projected = projectRows(rows, [archive, back], folderList({ dataUpdatedAt: 6000, hasMore: true }));
  assert.deepEqual(ids(projected), ["a"]);
});

test("a threaded row's unread count follows reading one of its messages", () => {
  const conversation = row("n", 1, { thread_id: "t1", is_seen: true, unread_in_thread: 2 });
  const olderUnread = row("o", 9, { thread_id: "t1" });
  const read = intent("mark_read", [olderUnread]);
  const [projected] = projectRows([conversation], [read], folderList({ threaded: true }));
  assert.equal(projected.unread_in_thread, 1);
  const stale = { ...read, state: "done" as const, doneAt: 4000 };
  const [fresh] = projectRows([conversation], [stale], folderList({ threaded: true }));
  assert.equal(fresh.unread_in_thread, 2, "a count read after the write already has it");
});

test("folder counts lose an archived unread message until counts are re-read", () => {
  const unread = row("u", 1);
  const seen = row("s", 2, { is_seen: true });
  const archive = intent("archive", [unread, seen], { state: "done", doneAt: 5000 });
  const deltas = folderCountDeltas([archive], 4000);
  assert.deepEqual(deltas.get(INBOX), { total: -2, unread: -1 });
  assert.equal(folderCountDeltas([archive], 6000).size, 0);
  const folder = { unread_count: 3, total_count: 10 };
  assert.deepEqual(projectCounts(folder, [INBOX], deltas), { unread_count: 2, total_count: 8 });
});

test("read then archive of one message counts it once", () => {
  const unread = row("u", 1);
  const read = intent("mark_read", [unread], { createdAt: 1 });
  const archive = intent("archive", [unread], { createdAt: 2 });
  assert.deepEqual(folderCountDeltas([read, archive], 0).get(INBOX), { total: -1, unread: -1 });
});

test("a count already holding the first of two intents gets only the second", () => {
  const unread = row("u", 1);
  const read = intent("mark_read", [unread], { createdAt: 1, state: "done", doneAt: 50 });
  const archive = intent("archive", [unread], { createdAt: 2 });
  assert.deepEqual(folderCountDeltas([read, archive], 100).get(INBOX), { total: -1, unread: 0 });
});

test("a move adds to the target folder's counts", () => {
  const unread = row("u", 1);
  const move = intent("move", [unread], { targetFolderId: WORK });
  assert.deepEqual(folderCountDeltas([move], 0).get(WORK), { total: 1, unread: 1 });
});

test("a conversation's messages take flag and move changes without being hidden", () => {
  const messages = [row("a", 3), row("b", 2)];
  const move = intent("move", [messages[0]], { targetFolderId: ARCHIVE });
  const star = intent("flag", [messages[1]]);
  const projected = projectThreadMessages(messages, [move, star], 0);
  assert.deepEqual(ids(projected), ["a", "b"]);
  assert.equal(projected[0].folder_id, ARCHIVE);
  assert.equal(projected[1].is_flagged, true);
});

test("failures are sorted into waiting, retrying, gone and refused", () => {
  assert.equal(classifyFailure(null), "network");
  assert.equal(classifyFailure(503), "retry");
  assert.equal(classifyFailure(429), "retry");
  assert.equal(classifyFailure(408), "retry");
  assert.equal(classifyFailure(404), "gone");
  assert.equal(classifyFailure(400), "terminal");
  assert.equal(classifyFailure(409), "terminal");
  assert.equal(retryDelay(1), 1000);
  assert.equal(retryDelay(3), 4000);
  assert.equal(retryDelay(20), 30_000);
  assert.equal(retryDelay(20, "network"), 10_000);
});

test("one intent per account is sent at a time, in creation order", () => {
  const a = intent("archive", [row("a", 1)], { createdAt: 1 });
  const b = intent("archive", [row("b", 2)], { createdAt: 2 });
  const other = intent("flag", [row("x", 3)], { createdAt: 3, accountId: "other" });
  assert.deepEqual(ids(sendableIntents([b, other, a], 10)), [a.id, other.id]);
  const inflight = { ...a, state: "inflight" as const };
  assert.deepEqual(ids(sendableIntents([inflight, b], 10)), []);
});

test("an intent waits behind an earlier one naming the same message", () => {
  const m = row("m", 1);
  const read = intent("mark_read", [m], { createdAt: 1, notBefore: 100 });
  const archive = intent("archive", [m], { createdAt: 2, accountId: "acct" });
  const unrelated = intent("flag", [row("z", 2)], { createdAt: 3 });
  // The read is backing off; the archive of the same message must not
  // overtake it, the unrelated flag may go.
  assert.deepEqual(ids(sendableIntents([read, archive, unrelated], 10)), [unrelated.id]);
  assert.equal(nextWakeAt([read, archive], 10), 100);
});

test("a reversal waits for the intent it reverses", () => {
  const archive = intent("archive", [row("a", 1)], { state: "inflight", accountId: "x" });
  const [back] = reversalsOf(archive, 50, () => "r");
  assert.deepEqual(ids(sendableIntents([archive, { ...back, accountId: "y" }], 100)), []);
});

test("undoing an archive moves each message back to its own folder and restores unread", () => {
  const unread = row("u", 1);
  const seenElsewhere = row("s", 2, { is_seen: true, folder_id: WORK });
  const archive = intent("archive", [unread, seenElsewhere]);
  let n = 0;
  const reversals = reversalsOf(archive, 900, () => `r${++n}`);
  assert.deepEqual(
    reversals.map((r) => [r.action, r.targetFolderId ?? null, ids(r.messages)]),
    [["move", INBOX, ["u"]], ["move", WORK, ["s"]], ["mark_unread", null, ["u"]]],
  );
  assert.ok(reversals.every((r) => r.reverses === archive.id));
  assert.ok(reversals[2].createdAt > reversals[0].createdAt, "unread goes after the move");
});

test("undoing a conversation archive uses the messages the server reported", () => {
  const archive = intent("archive", [row("n", 1)], {
    expandThreads: true, sources: [{ id: "n", folderId: INBOX }, { id: "o", folderId: INBOX }],
  });
  const [move] = reversalsOf(archive, 900, () => "r");
  assert.deepEqual(ids(move.messages), ["n", "o"]);
});

test("undoing a flag change only touches messages it changed", () => {
  const starred = row("s", 1, { is_flagged: true });
  const plain = row("p", 2);
  const [unflag] = reversalsOf(intent("flag", [starred, plain]), 900, () => "r");
  assert.equal(unflag.action, "unflag");
  assert.deepEqual(ids(unflag.messages), ["p"]);
  assert.deepEqual(reversalsOf(intent("flag", [starred]), 900, () => "r"), []);
});

test("the undo stack is bounded and forgets old steps", () => {
  const entries: UndoEntry[] = Array.from({ length: UNDO_STACK_LIMIT + 5 }, (_, i) => ({
    id: `u${i}`, label: "x", createdAt: 10_000_000 + i, intents: [],
  }));
  const pruned = pruneUndo(entries, 10_000_000 + 100);
  assert.equal(pruned.length, UNDO_STACK_LIMIT);
  assert.equal(pruned[pruned.length - 1].id, `u${UNDO_STACK_LIMIT + 4}`);
  assert.deepEqual(pruneUndo(entries, 10_000_000 + UNDO_MAX_AGE_MS + 1000), []);
});

test("two tabs' ledgers merge to the more advanced copy of each intent", () => {
  const a = intent("archive", [row("a", 1)]);
  const b = intent("flag", [row("b", 1)]);
  const theirsA = { ...a, state: "done" as const, doneAt: 9, updatedAt: a.updatedAt + 5 };
  const merged = mergeIntents([a, b], [theirsA], new Set([b.id]));
  assert.deepEqual(ids(merged), [a.id]);
  assert.equal(merged[0].state, "done");
});
