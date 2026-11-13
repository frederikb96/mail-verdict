import { test } from "node:test";
import assert from "node:assert/strict";
import {
  changedByCategory,
  changedCount,
  draftValue,
  editKey,
  withoutCategories,
} from "./settings-draft.ts";

const server = {
  mail: { notify_wait_seconds: 120, bell_badge_counts_new_mail: true },
  orders: { decide_model: "a", write_model: "b" },
};

test("an edit shadows the server value", () => {
  const edits = { [editKey("mail", "notify_wait_seconds")]: 30 };
  assert.equal(draftValue(server, edits, "mail", "notify_wait_seconds"), 30);
  assert.equal(draftValue(server, edits, "mail", "bell_badge_counts_new_mail"), true);
});

test("an edit set back to the server value is not a change", () => {
  const edits = { [editKey("mail", "notify_wait_seconds")]: 120 };
  assert.equal(changedCount(server, edits), 0);
});

test("changes are grouped by category and keep their key names", () => {
  const edits = {
    [editKey("orders", "decide_model")]: "c",
    [editKey("orders", "write_model")]: "d",
    [editKey("mail", "notify_wait_seconds")]: 30,
  };
  assert.deepEqual(changedByCategory(server, edits), {
    orders: { decide_model: "c", write_model: "d" },
    mail: { notify_wait_seconds: 30 },
  });
  assert.equal(changedCount(server, edits), 3);
});

test("objects compare by content", () => {
  const doc = { orders: { filter: { include: { subject: ["a"] } } } };
  const edits = { [editKey("orders", "filter")]: { include: { subject: ["a"] } } };
  assert.equal(changedCount(doc, edits), 0);
});

test("saving a category drops only its edits", () => {
  const edits = {
    [editKey("orders", "decide_model")]: "c",
    [editKey("mail", "notify_wait_seconds")]: 30,
  };
  assert.deepEqual(withoutCategories(edits, ["orders"]), {
    [editKey("mail", "notify_wait_seconds")]: 30,
  });
});
