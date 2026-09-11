import { test } from "node:test";
import assert from "node:assert/strict";
import { chunkIntoPages, mergeRefreshedWindow, windowRefreshLimit } from "./mail-list-window.ts";

/** Rows one minute apart, newest first -- `row(0)` is the newest. */
function row(minutesAgo: number, id = `m${String(1000 - minutesAgo).padStart(4, "0")}`) {
  const at = new Date(Date.UTC(2026, 8, 11, 12, 0) - minutesAgo * 60_000).toISOString();
  return { id, received_at: at };
}
const ids = (rows: { id: string }[]) => rows.map((r) => r.id);

test("a new message at the top is added and the window keeps its old last row", () => {
  const current = [row(1), row(2), row(3)];
  const fresh = [row(0), row(1), row(2), row(3), row(4), row(5)];
  const merged = mergeRefreshedWindow(current, fresh, true, true);
  assert.deepEqual(ids(merged.rows), ids([row(0), row(1), row(2), row(3)]));
  assert.equal(merged.hasMore, true);
});

test("a message that left the folder is dropped, whatever its position", () => {
  const current = [row(1), row(2), row(3)];
  const fresh = [row(1), row(3), row(4)];
  const merged = mergeRefreshedWindow(current, fresh, false, true);
  assert.deepEqual(ids(merged.rows), ids([row(1), row(3)]));
  assert.equal(merged.hasMore, true, "row(4) was left for paging");
});

test("the old last row leaving still bounds the window by its position", () => {
  const current = [row(1), row(2), row(3)];
  const fresh = [row(1), row(2), row(4), row(5)];
  const merged = mergeRefreshedWindow(current, fresh, true, true);
  assert.deepEqual(ids(merged.rows), ids([row(1), row(2)]));
  assert.equal(merged.hasMore, true);
});

test("a fully loaded folder stays fully loaded", () => {
  const current = [row(1), row(2)];
  const fresh = [row(0), row(1), row(2)];
  const merged = mergeRefreshedWindow(current, fresh, false, false);
  assert.deepEqual(ids(merged.rows), ids(fresh));
  assert.equal(merged.hasMore, false);
});

test("rows deeper than one refresh reaches are kept below it", () => {
  const current = [row(1), row(2), row(3), row(4), row(5)];
  const fresh = [row(0), row(1), row(2)];
  const merged = mergeRefreshedWindow(current, fresh, true, true);
  assert.deepEqual(ids(merged.rows), ids([row(0), row(1), row(2), row(3), row(4), row(5)]));
  assert.equal(merged.hasMore, true);
});

test("a row removed inside the reach of a capped refresh is dropped, one below it kept", () => {
  const current = [row(1), row(2), row(3), row(4), row(5)];
  const fresh = [row(1), row(3)];
  const merged = mergeRefreshedWindow(current, fresh, true, false);
  assert.deepEqual(ids(merged.rows), ids([row(1), row(3), row(4), row(5)]));
  assert.equal(merged.hasMore, false);
});

test("a conversation moving to the top changes its row id without duplicating it", () => {
  // Grouped by conversation, a thread's row is its newest message -- a
  // reply arriving replaces the row's id and moves it to the top.
  const current = [row(1), row(2, "old-representative"), row(3)];
  const fresh = [row(0, "new-representative"), row(1), row(3)];
  const merged = mergeRefreshedWindow(current, fresh, false, false);
  assert.deepEqual(ids(merged.rows), ["new-representative", row(1).id, row(3).id]);
});

test("an empty cache takes the fresh read as it is", () => {
  const fresh = [row(0), row(1)];
  assert.deepEqual(mergeRefreshedWindow([], fresh, true, false), { rows: fresh, hasMore: true });
});

test("a message with no date sits above every dated one, as the server orders it", () => {
  const undated = { id: "undated", received_at: null };
  const current = [row(1), row(2)];
  const fresh = [undated, row(1), row(2)];
  const merged = mergeRefreshedWindow(current, fresh, false, false);
  assert.deepEqual(ids(merged.rows), ["undated", row(1).id, row(2).id]);
});

test("the refresh reads the loaded window plus slack, capped", () => {
  assert.equal(windowRefreshLimit(0), 50);
  assert.equal(windowRefreshLimit(150), 200);
  assert.equal(windowRefreshLimit(5000), 1000);
});

test("pages are cut at the list's own page size, and never zero pages", () => {
  assert.deepEqual(chunkIntoPages([]), [[]]);
  const rows = Array.from({ length: 120 }, (_, i) => i);
  assert.deepEqual(chunkIntoPages(rows).map((p) => p.length), [50, 50, 20]);
});
