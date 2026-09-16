import { test } from "node:test";
import assert from "node:assert/strict";
import { DONE_RETENTION_MS, FAILED_RETENTION_MS, type MailIntent } from "./mail-intents.ts";

/** A browser's storage, enough of it for the ledger to persist through --
 * the merge with the stored copy is where a single tab's writes can undo
 * themselves, so testing without it tests nothing. */
class MemoryStorage {
  data = new Map<string, string>();
  failWrites = false;
  getItem(key: string) {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    if (this.failWrites) throw new Error("QuotaExceededError");
    this.data.set(key, value);
  }
  removeItem(key: string) {
    this.data.delete(key);
  }
}
const localStorage = new MemoryStorage();
Object.assign(globalThis, {
  window: {
    localStorage, sessionStorage: new MemoryStorage(),
    addEventListener: () => {}, setTimeout: () => 0,
  },
});

const ledger = await import("./intent-ledger.ts");
const STORAGE_KEY = "mail-verdict-mail-intents";

function intent(id: string, now = Date.now()): MailIntent {
  return {
    id, accountId: "acct", action: "archive", bulk: false, createdAt: now, state: "pending",
    attempts: 0, notBefore: now, updatedAt: now, generation: 0,
    messages: [{ id: `m-${id}`, folderId: "inbox", isSeen: false, isFlagged: false, threadId: null,
      row: { id: `m-${id}` } as never }],
  };
}

function reset(): void {
  const { intents, undo, undoRequests } = ledger.getLedgerSnapshot();
  ledger.retireIntents(intents.map((i) => i.id));
  for (const entry of undo) ledger.takeUndo(entry.id);
  for (const request of undoRequests) ledger.consumeUndoRequest(request.id);
  localStorage.failWrites = false;
}

function stored(): { intents: MailIntent[] } {
  return JSON.parse(localStorage.getItem(STORAGE_KEY)!);
}

test("an action is one undo step, and only the undo copy keeps row snapshots", () => {
  reset();
  const entry = ledger.addIntents([intent("a1"), intent("a2")], "2 messages archived");
  assert.ok(entry);
  const { intents, undo } = ledger.getLedgerSnapshot();
  assert.equal(intents.length, 2);
  assert.equal(intents[0].messages[0].row, undefined);
  assert.ok(undo[0].intents[0].messages[0].row);
  assert.equal(ledger.takeUndo()?.id, entry!.id);
  assert.equal(ledger.takeUndo(), null, "the stack is empty once taken");
});

test("Retry on a refused intent sticks, however the stored copy ranks it", () => {
  reset();
  ledger.addIntents([intent("r1")]);
  ledger.updateIntent("r1", { state: "failed", lastError: "403" });
  assert.equal(stored().intents.find((i) => i.id === "r1")?.state, "failed");

  ledger.restartIntent("r1", { resend: false });

  const live = ledger.getLedgerSnapshot().intents.find((i) => i.id === "r1");
  assert.equal(live?.state, "pending");
  assert.equal(live?.generation, 1);
  assert.equal(stored().intents.find((i) => i.id === "r1")?.state, "pending");
});

test("a resent intent keeps its attempts, and a new generation forgets a refusal", () => {
  reset();
  ledger.addIntents([intent("s1")]);
  ledger.updateIntent("s1", { state: "held", attempts: 3, firstSentAt: 5 });
  ledger.restartIntent("s1", { resend: true });
  const resent = ledger.getLedgerSnapshot().intents.find((i) => i.id === "s1");
  assert.equal(resent?.attempts, 3);
  assert.equal(resent?.firstSentAt, 5);
  ledger.updateIntent("s1", { state: "failed", refused: true });
  ledger.restartIntent("s1", { resend: false });
  assert.equal(ledger.getLedgerSnapshot().intents.find((i) => i.id === "s1")?.refused, undefined);
});

test("what the server answered reaches the undo copy", () => {
  reset();
  ledger.addIntents([intent("b1")], "Archived");
  ledger.updateIntent("b1", {
    state: "done", doneAt: 42, landedFolderId: "archive",
    sources: [{ id: "m-b1", folderId: "inbox" }],
  });
  const [entry] = ledger.getLedgerSnapshot().undo;
  assert.equal(entry.intents[0].doneAt, 42);
  assert.equal(entry.intents[0].landedFolderId, "archive");
  assert.deepEqual(entry.intents[0].sources, [{ id: "m-b1", folderId: "inbox" }]);
});

test("another tab's answer reaches this tab's undo copy", () => {
  reset();
  const entry = ledger.addIntents([intent("w1")], "Archived")!;
  // The sending tab recorded the answer, and has since retired the intent.
  const answered = {
    ...intent("w1"), state: "done" as const, doneAt: 7, landedFolderId: "archive",
    updatedAt: Date.now() + 1000,
  };
  const { messages: _rows, ...withoutRows } = answered;
  localStorage.data.set(STORAGE_KEY, JSON.stringify({
    v: 1, intents: [], retired: { w1: Date.now() },
    undo: [{ ...entry, intents: [{ ...withoutRows, messages: [{ ...answered.messages[0], row: undefined }] }] }],
  }));
  ledger.addIntents([intent("w2")]);
  const [copy] = ledger.getLedgerSnapshot().undo[0].intents;
  assert.equal(copy.state, "done");
  assert.equal(copy.landedFolderId, "archive");
  assert.ok(copy.messages[0].row, "the row snapshot this tab kept survives");
});

test("an undo asked for stops its intents showing until it is carried out", () => {
  reset();
  const entry = ledger.addIntents([intent("u1")], "Archived")!;
  ledger.takeUndo(entry.id);
  ledger.requestUndo(entry);
  const snapshot = ledger.getLedgerSnapshot();
  assert.equal(snapshot.intents.length, 1, "the intent itself stays for the sending tab");
  assert.deepEqual(ledger.projectableIntents(snapshot), []);
  ledger.consumeUndoRequest(snapshot.undoRequests[0].id);
  assert.equal(ledger.getLedgerSnapshot().undoRequests.length, 0);
});

test("Ctrl+Z takes back this tab's own steps only", () => {
  reset();
  const mine = ledger.addIntents([intent("t1")], "Archived")!;
  assert.equal(ledger.takeUndo(undefined, "another tab"), null);
  assert.equal(ledger.takeUndo(undefined, ledger.currentTabId())?.id, mine.id);
});

test("a failed action's undo step is dropped", () => {
  reset();
  ledger.addIntents([intent("c1")], "Archived");
  ledger.dropUndoFor("c1");
  assert.equal(ledger.getLedgerSnapshot().undo.length, 0);
});

test("the sweep retires done and long-failed or held intents only", () => {
  reset();
  const now = Date.now();
  ledger.addIntents([intent("d1"), intent("d2"), intent("d3"), intent("d4"), intent("d5")]);
  ledger.updateIntent("d1", { state: "done", doneAt: now - DONE_RETENTION_MS - 1 });
  ledger.updateIntent("d2", { state: "done", doneAt: now });
  ledger.updateIntent("d3", { state: "failed" });
  ledger.updateIntent("d5", { state: "held" });
  ledger.sweepLedger(now);
  assert.deepEqual(ledger.getLedgerSnapshot().intents.map((i) => i.id), ["d2", "d3", "d4", "d5"]);
  ledger.sweepLedger(now + FAILED_RETENTION_MS + 1000);
  assert.deepEqual(ledger.getLedgerSnapshot().intents.map((i) => i.id), ["d4"]);
});

test("a full storage is said, and no stale stored copy is merged back over the ledger", () => {
  reset();
  ledger.addIntents([intent("q1")]);
  ledger.updateIntent("q1", { state: "done", doneAt: Date.now() });
  localStorage.failWrites = true;
  ledger.retireIntents(["q1"]);
  assert.equal(ledger.getLedgerSnapshot().persistenceFailed, true);
  // What storage still holds is from before; this tab can no longer keep it
  // current, so reading it back would resurrect what was settled since.
  const ghost = { ...intent("ghost"), state: "pending" as const };
  localStorage.data.set(STORAGE_KEY, JSON.stringify({ v: 1, intents: [ghost], undo: [], retired: {} }));
  ledger.addIntents([intent("q2")]);
  assert.deepEqual(ledger.getLedgerSnapshot().intents.map((i) => i.id), ["q2"]);
});

test("a stored ledger of the wrong shape does not break loading", () => {
  reset();
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ v: 1, intents: [{ id: 3 }], undo: "x" }));
  ledger.addIntents([intent("v1")]);
  assert.deepEqual(ledger.getLedgerSnapshot().intents.map((i) => i.id), ["v1"]);
});
