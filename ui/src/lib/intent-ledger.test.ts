import { test } from "node:test";
import assert from "node:assert/strict";
import {
  addIntents,
  dropUndoFor,
  getLedgerSnapshot,
  retireIntents,
  sweepLedger,
  takeUndo,
  updateIntent,
} from "./intent-ledger.ts";
import { DONE_RETENTION_MS, FAILED_RETENTION_MS, type MailIntent } from "./mail-intents.ts";

function intent(id: string, now = Date.now()): MailIntent {
  return {
    id, accountId: "acct", action: "archive", bulk: false, createdAt: now, state: "pending",
    attempts: 0, notBefore: now, updatedAt: now,
    messages: [{ id: `m-${id}`, folderId: "inbox", isSeen: false, isFlagged: false, threadId: null,
      row: { id: `m-${id}` } as never }],
  };
}

function reset(): void {
  const { intents, undo } = getLedgerSnapshot();
  retireIntents(intents.map((i) => i.id));
  for (const entry of undo) takeUndo(entry.id);
}

test("an action is one undo step, and only the undo copy keeps row snapshots", () => {
  reset();
  const entry = addIntents([intent("a1"), intent("a2")], "2 messages archived");
  assert.ok(entry);
  const { intents, undo } = getLedgerSnapshot();
  assert.equal(intents.length, 2);
  assert.equal(intents[0].messages[0].row, undefined);
  assert.ok(undo[0].intents[0].messages[0].row);
  assert.equal(takeUndo()?.id, entry!.id);
  assert.equal(takeUndo(), null, "the stack is empty once taken");
});

test("what the server answered reaches the undo copy", () => {
  reset();
  addIntents([intent("b1")], "Archived");
  updateIntent("b1", { state: "done", doneAt: 42, sources: [{ id: "m-b1", folderId: "inbox" }] });
  const [entry] = getLedgerSnapshot().undo;
  assert.equal(entry.intents[0].doneAt, 42);
  assert.deepEqual(entry.intents[0].sources, [{ id: "m-b1", folderId: "inbox" }]);
});

test("a failed action's undo step is dropped", () => {
  reset();
  addIntents([intent("c1")], "Archived");
  dropUndoFor("c1");
  assert.equal(getLedgerSnapshot().undo.length, 0);
});

test("the sweep retires done and long-failed intents only", () => {
  reset();
  const now = Date.now();
  addIntents([intent("d1"), intent("d2"), intent("d3"), intent("d4")]);
  updateIntent("d1", { state: "done", doneAt: now - DONE_RETENTION_MS - 1 });
  updateIntent("d2", { state: "done", doneAt: now });
  updateIntent("d3", { state: "failed" });
  sweepLedger(now);
  assert.deepEqual(getLedgerSnapshot().intents.map((i) => i.id), ["d2", "d3", "d4"]);
  sweepLedger(now + FAILED_RETENTION_MS + 1000);
  assert.deepEqual(getLedgerSnapshot().intents.map((i) => i.id).sort(), ["d4"]);
});
