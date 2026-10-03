import { test } from "node:test";
import assert from "node:assert/strict";
import {
  SWIPE_COMMIT_PX,
  SWIPE_LOCK_PX,
  SWIPE_MAX_PX,
  swipeAction,
  swipeAxis,
  swipeOffset,
} from "./order-swipe.ts";

test("a small movement stays undecided", () => {
  assert.equal(swipeAxis(4, 3), "pending");
  assert.equal(swipeAxis(-(SWIPE_LOCK_PX - 1), 0), "pending");
});

test("mostly sideways is horizontal, mostly up or down is vertical", () => {
  assert.equal(swipeAxis(30, 8), "horizontal");
  assert.equal(swipeAxis(-30, 8), "horizontal");
  assert.equal(swipeAxis(8, 30), "vertical");
  assert.equal(swipeAxis(12, 12), "vertical");
});

test("right-to-left past the threshold favorites, left-to-right closes", () => {
  assert.equal(swipeAction(-SWIPE_COMMIT_PX), "favorite");
  assert.equal(swipeAction(-200), "favorite");
  assert.equal(swipeAction(SWIPE_COMMIT_PX), "close");
  assert.equal(swipeAction(200), "close");
});

test("short of the threshold commits nothing", () => {
  assert.equal(swipeAction(0), null);
  assert.equal(swipeAction(SWIPE_COMMIT_PX - 1), null);
  assert.equal(swipeAction(-(SWIPE_COMMIT_PX - 1)), null);
});

test("the row follows the finger one-to-one up to the threshold, then resists, capped", () => {
  assert.equal(swipeOffset(40), 40);
  assert.equal(swipeOffset(-40), -40);
  const past = swipeOffset(SWIPE_COMMIT_PX + 40);
  assert.ok(past > SWIPE_COMMIT_PX && past < SWIPE_MAX_PX);
  assert.ok(swipeOffset(2000) <= SWIPE_MAX_PX);
  assert.equal(swipeOffset(-(SWIPE_COMMIT_PX + 40)), -past);
});
