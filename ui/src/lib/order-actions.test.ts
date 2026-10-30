import { test } from "node:test";
import assert from "node:assert/strict";
import { orderActions, orderToggle } from "./order-actions.ts";

const plain = { is_favorite: false, is_open: true, is_sealed: false, kind: "order" } as const;

test("an open, plain order offers Favorite, Close, Seal and the rest", () => {
  const labels = orderActions(plain).map((a) => a.label);
  assert.deepEqual(labels, [
    "Favorite",
    "Close",
    "Seal",
    "Mark as ticket",
    "Mark as other",
    "Rewrite summary",
    "Merge into another order…",
    "Delete order…",
  ]);
});

test("each toggle flips its label with the order's state", () => {
  const labels = orderActions({ ...plain, is_favorite: true, is_open: false, is_sealed: true }).map(
    (a) => a.label,
  );
  assert.deepEqual(labels.slice(0, 3), ["Unfavorite", "Reopen", "Unseal"]);
});

test("only delete is destructive", () => {
  const destructive = orderActions(plain).filter((a) => a.destructive);
  assert.deepEqual(destructive.map((a) => a.id), ["delete"]);
});

test("closing an open order patches is_open false, reopening patches it true", () => {
  assert.deepEqual(orderToggle(plain, "close")?.patch, { is_open: false });
  assert.deepEqual(orderToggle({ ...plain, is_open: false }, "close")?.patch, { is_open: true });
});

test("favorite and seal patch only their own field", () => {
  assert.deepEqual(orderToggle(plain, "favorite")?.patch, { is_favorite: true });
  assert.deepEqual(orderToggle({ ...plain, is_favorite: true }, "favorite")?.patch, {
    is_favorite: false,
  });
  assert.deepEqual(orderToggle(plain, "seal")?.patch, { is_sealed: true });
  assert.deepEqual(orderToggle({ ...plain, is_sealed: true }, "seal")?.patch, { is_sealed: false });
});

test("rewrite, merge and delete are not flag flips", () => {
  for (const id of ["rewrite", "merge", "delete"] as const) {
    assert.equal(orderToggle(plain, id), null);
  }
});

test("the kind actions leave out the kind the order already has", () => {
  const ids = (kind: "ticket" | "order" | "other") =>
    orderActions({ ...plain, kind })
      .map((a) => a.id)
      .filter((id) => id.startsWith("kind_"));
  assert.deepEqual(ids("order"), ["kind_ticket", "kind_other"]);
  assert.deepEqual(ids("ticket"), ["kind_order", "kind_other"]);
  assert.deepEqual(ids("other"), ["kind_ticket", "kind_order"]);
});

test("a kind action patches only kind", () => {
  assert.deepEqual(orderToggle(plain, "kind_ticket")?.patch, { kind: "ticket" });
  assert.deepEqual(orderToggle(plain, "kind_other")?.patch, { kind: "other" });
});
