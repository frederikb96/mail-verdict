import { test } from "node:test";
import assert from "node:assert/strict";
import { moveWithinGroup, reorderWithinGroup } from "./rule-order.ts";

const order = ["classify", "a", "x", "b", "y", "c"];

test("a move inside a group leaves other rules in their slots", () => {
  assert.deepEqual(reorderWithinGroup(order, ["a", "b", "c"], ["c", "a", "b"]), [
    "classify", "c", "x", "a", "y", "b",
  ]);
});

test("moving one place swaps with the group's neighbour, not the list's", () => {
  assert.deepEqual(moveWithinGroup(order, ["a", "b", "c"], "b", -1), [
    "classify", "b", "x", "a", "y", "c",
  ]);
  assert.deepEqual(moveWithinGroup(order, ["a", "b", "c"], "b", 1), [
    "classify", "a", "x", "c", "y", "b",
  ]);
});

test("moving past either end changes nothing", () => {
  assert.equal(moveWithinGroup(order, ["a", "b", "c"], "a", -1), order);
  assert.equal(moveWithinGroup(order, ["a", "b", "c"], "c", 1), order);
  assert.equal(moveWithinGroup(order, ["a", "b", "c"], "x", 1), order);
});
