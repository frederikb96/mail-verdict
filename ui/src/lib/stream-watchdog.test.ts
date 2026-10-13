import { test } from "node:test";
import assert from "node:assert/strict";
import { STALE_AFTER_MS, streamIsStale } from "./stream-watchdog.ts";

test("a stream that has pinged and then falls silent is stale", () => {
  assert.equal(streamIsStale({ lastAt: 1000, pinged: true }, 1000 + STALE_AFTER_MS), false);
  assert.equal(streamIsStale({ lastAt: 1000, pinged: true }, 1001 + STALE_AFTER_MS), true);
});

test("a stream that never pinged is never judged by its silence", () => {
  assert.equal(streamIsStale({ lastAt: 0, pinged: false }, 10 * STALE_AFTER_MS), false);
});
