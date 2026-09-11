import { test } from "node:test";
import assert from "node:assert/strict";
import { idsToClose } from "./live-notifications.ts";

test("a tag whose alert is no longer live is closed", () => {
  assert.deepEqual(idsToClose(["a", "b"], new Set(["a"])), ["b"]);
});

test("every tag still live stays open", () => {
  assert.deepEqual(idsToClose(["a", "b"], new Set(["a", "b"])), []);
});

test("no shown notifications is a no-op", () => {
  assert.deepEqual(idsToClose([], new Set(["a"])), []);
});

test("duplicate tags are only reported once", () => {
  assert.deepEqual(idsToClose(["a", "a", "b"], new Set()), ["a", "b"]);
});
