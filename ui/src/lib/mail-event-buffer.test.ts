import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ECHO_GRACE_MS,
  ECHO_HOLD_MAX_MS,
  bufferMailEvent,
  ownEcho,
  planMailRefresh,
  triageMailEvents,
} from "./mail-event-buffer.ts";
import type { MailIntent } from "./mail-intents.ts";
import type { SSEEvent } from "../types/api.ts";

function intent(over: Partial<MailIntent> & { ids?: string[] }): MailIntent {
  const { ids = ["m1"], ...rest } = over;
  return {
    id: "i1", accountId: "acct", action: "archive", bulk: false, createdAt: 1000,
    state: "done", attempts: 1, notBefore: 1000, updatedAt: 1000, generation: 0,
    firstSentAt: 1000, doneAt: 1300,
    messages: ids.map((id) => ({ id, folderId: "inbox", isSeen: true, isFlagged: false, threadId: null })),
    ...rest,
  };
}

const payload = (fields: Partial<SSEEvent>): SSEEvent => ({ timestamp: "", ...fields });

const moved = (id: string, at: number) =>
  bufferMailEvent("updated", payload({ id, folder_id: "archive", old_folder_id: "inbox", changed: ["folder_id"] }), at);

test("an event arriving before the answer is the echo of the settled request", () => {
  assert.equal(ownEcho("m1", 1200, [intent({})]), "settled");
});

test("an event shortly after the answer is still its echo, a late one is not", () => {
  const done = [intent({})];
  assert.equal(ownEcho("m1", 1300 + ECHO_GRACE_MS, done), "settled");
  assert.equal(ownEcho("m1", 1300 + ECHO_GRACE_MS + 1, done), "none");
});

test("an event from before the request left is another client's", () => {
  assert.equal(ownEcho("m1", 999, [intent({})]), "none");
});

test("an event for a message the intent never touched is not an echo", () => {
  assert.equal(ownEcho("m2", 1200, [intent({})]), "none");
});

test("messages a conversation action reached count as touched", () => {
  const bulk = [intent({ sources: [{ id: "m9", folderId: "inbox" }], skippedIds: ["m8"] })];
  assert.equal(ownEcho("m9", 1200, bulk), "settled");
  assert.equal(ownEcho("m8", 1200, bulk), "settled");
});

test("a failed or pending intent has no echo", () => {
  assert.equal(ownEcho("m1", 1200, [intent({ state: "failed" })]), "none");
  assert.equal(ownEcho("m1", 1200, [intent({ state: "pending", firstSentAt: undefined })]), "none");
});

test("settled echoes are dropped from a burst, other events kept", () => {
  const events = [moved("m1", 1200), moved("other", 1200)];
  const { apply, hold } = triageMailEvents(events, [intent({})], 1700);
  assert.deepEqual(apply.map((e) => e.id), ["other"]);
  assert.equal(hold.length, 0);
});

test("an event for a request still out is held, then applied once held too long", () => {
  const out = [intent({ state: "inflight", doneAt: undefined })];
  const event = moved("m1", 1200);
  assert.deepEqual(triageMailEvents([event], out, 1700), { apply: [], hold: [event] });
  const late = triageMailEvents([event], out, 1200 + ECHO_HOLD_MAX_MS);
  assert.deepEqual(late, { apply: [event], hold: [] });
});

const archived = intent({ id: "a", doneAt: 1300 });
const undoInflight = intent({
  id: "u", reverses: "a", action: "move", state: "inflight", firstSentAt: 1350, doneAt: undefined,
});

test("the echo of a move undone at once is dropped, not recorded over the undo", () => {
  assert.equal(ownEcho("m1", 1400, [archived, undoInflight]), "settled");
  const { apply, hold } = triageMailEvents([moved("m1", 1400)], [archived, undoInflight], 1900);
  assert.equal(apply.length + hold.length, 0);
});

test("an echo arriving after its answer's grace waits for a request still out, then is dropped", () => {
  const late = moved("m1", 1300 + ECHO_GRACE_MS + 500);
  assert.equal(triageMailEvents([late], [archived, undoInflight], 3500).hold.length, 1);
  const undone = { ...undoInflight, state: "done" as const, doneAt: 3400 };
  const after = triageMailEvents([late], [archived, undone], 3600);
  assert.equal(after.apply.length + after.hold.length, 0);
});

test("a plan folds events the way the individual handlers did", () => {
  const plan = planMailRefresh([
    bufferMailEvent("new", payload({ id: "n1", folder_id: "inbox" }), 1),
    bufferMailEvent("updated", payload({ id: "u1", folder_id: "inbox", changed: ["is_seen"] }), 2),
    moved("m1", 3),
    bufferMailEvent("deleted", payload({ id: "d1", folder_id: "trash" }), 4),
  ]);
  assert.deepEqual([...plan.updatedIds], ["u1"]);
  assert.deepEqual([...plan.removedIds], ["m1", "d1"]);
  assert.equal(plan.newOrMoved, true);
  assert.equal(plan.folderCounts, true);
  assert.deepEqual([...(plan.folders ?? [])].sort(), ["archive", "inbox", "trash"]);
});

test("an event naming no folder, or unreadable, widens the refresh to every list", () => {
  assert.equal(planMailRefresh([bufferMailEvent("new", payload({ id: "n1" }), 1)]).folders, null);
  const unreadable = planMailRefresh([bufferMailEvent("updated", null, 1)]);
  assert.equal(unreadable.folders, null);
  assert.equal(unreadable.newOrMoved, true);
});

test("a plan without events asks for nothing", () => {
  const plan = planMailRefresh([]);
  assert.equal(plan.newOrMoved || plan.folderCounts, false);
  assert.equal(plan.updatedIds.size + plan.removedIds.size, 0);
});
