import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildTimelineRows,
  scrollTopForAnchor,
  timelineCenterTop,
  timelineSplitIndex,
  todayString,
  topAnchor,
} from "./order-timeline.ts";

const item = (id: string, timeline_date: string | null) => ({ id, timeline_date });

test("todayString is the local calendar date, zero padded", () => {
  assert.equal(todayString(new Date(2026, 0, 5, 23, 59)), "2026-01-05");
  assert.equal(todayString(new Date(2026, 10, 30, 0, 1)), "2026-11-30");
});

test("the split sits after everything dated today or later", () => {
  const items = [item("a", "2026-11-03"), item("b", "2026-10-30"), item("c", "2026-10-29")];
  assert.equal(timelineSplitIndex(items, "2026-10-30"), 2);
  assert.equal(timelineSplitIndex(items, "2026-11-04"), 0);
  assert.equal(timelineSplitIndex(items, "2026-10-01"), 3);
});

test("entries without a date count as past, below the line", () => {
  const items = [item("a", "2026-11-03"), item("b", null)];
  assert.equal(timelineSplitIndex(items, "2026-10-30"), 1);
});

test("rows carry the today marker exactly at the split", () => {
  const rows = buildTimelineRows([item("a", "2026-11-03"), item("b", "2026-10-01")], "2026-10-30");
  assert.deepEqual(
    rows.map((r) => (r.type === "today" ? "today" : r.order.id)),
    ["a", "today", "b"],
  );
  const empty = buildTimelineRows([], "2026-10-30");
  assert.deepEqual(empty.map((r) => r.type), ["today"]);
});

test("the centre puts the divider's middle in the middle of the viewport", () => {
  // 10 rows above a 40px line, 124px rows: the line spans 1240..1280.
  const top = timelineCenterTop({
    splitIndex: 10,
    itemCount: 30,
    rowHeight: 124,
    dividerHeight: 40,
    viewportHeight: 600,
  });
  assert.equal(top, 1240 + 20 - 300);
});

test("the centre is clamped to the scrollable range and whole pixels", () => {
  const near = {
    itemCount: 3,
    rowHeight: 124,
    dividerHeight: 40,
    viewportHeight: 601,
  };
  assert.equal(timelineCenterTop({ ...near, splitIndex: 0 }), 0);
  // content = 3*124 + 40 = 412 < viewport: nothing to scroll.
  assert.equal(timelineCenterTop({ ...near, splitIndex: 3 }), 0);
  const long = timelineCenterTop({
    splitIndex: 5,
    itemCount: 6,
    rowHeight: 124,
    dividerHeight: 40,
    viewportHeight: 601,
  });
  assert.equal(Number.isInteger(long), true);
  assert.ok(long <= 6 * 124 + 40 - 601);
});

test("the top anchor names the row under the viewport's top edge and its offset", () => {
  const items = [item("a", "2026-11-03"), item("b", "2026-10-31"), item("c", "2026-10-01")];
  const geometry = { rowHeight: 100, dividerHeight: 40, splitIndex: 2 };
  assert.deepEqual(topAnchor(items, 130, geometry), { id: "b", offset: 30 });
  // inside the divider: the next row below it anchors, offset negative
  assert.deepEqual(topAnchor(items, 210, geometry), { id: "c", offset: -30 });
  assert.equal(topAnchor([], 0, geometry), null);
});

test("an arrival above the reader shifts scrollTop by exactly the rows added", () => {
  const before = [item("b", "2026-10-31"), item("c", "2026-10-01")];
  const geometry = { rowHeight: 100, dividerHeight: 40, splitIndex: 1 };
  const anchor = topAnchor(before, 60, geometry);
  assert.deepEqual(anchor, { id: "b", offset: 60 });
  const after = [item("n", "2026-11-09"), item("b", "2026-10-31"), item("c", "2026-10-01")];
  const target = scrollTopForAnchor(after, anchor!, { ...geometry, splitIndex: 2 });
  assert.equal(target, 60 + 100);
});

test("an anchor whose row is gone has no target", () => {
  const geometry = { rowHeight: 100, dividerHeight: 40, splitIndex: 0 };
  assert.equal(scrollTopForAnchor([item("x", null)], { id: "gone", offset: 5 }, geometry), null);
});
