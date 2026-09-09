import { test } from "node:test";
import assert from "node:assert/strict";
import {
  computeFetchWindow,
  computeRenderRange,
  monthsForRenderRange,
  sameMonthSet,
  sameRange,
  withMonthMargin,
} from "./month-window.ts";
import { WEEK_INDEX_MAX, WEEK_INDEX_MIN, dateToWeekIndex } from "../../lib/dates.ts";

const ROW_HEIGHT = 120;
const MARGIN_ROWS = 8;

test("computeRenderRange centers on scrollTop with margin on both sides", () => {
  const anchor = dateToWeekIndex(new Date(2026, 8, 9));
  const scrollTop = (anchor - WEEK_INDEX_MIN) * ROW_HEIGHT;
  const range = computeRenderRange(scrollTop, 720, ROW_HEIGHT, MARGIN_ROWS, WEEK_INDEX_MIN, WEEK_INDEX_MAX);
  // 720 / 120 = 6 rows visible; margin 8 rows each side.
  assert.equal(range.start, anchor - MARGIN_ROWS);
  assert.equal(range.end, anchor + 6 + MARGIN_ROWS);
});

test("computeRenderRange clamps to the min/max week bounds", () => {
  const range = computeRenderRange(0, 720, ROW_HEIGHT, MARGIN_ROWS, WEEK_INDEX_MIN, WEEK_INDEX_MAX);
  assert.equal(range.start, WEEK_INDEX_MIN);
});

test("computeRenderRange never divides by a zero row height", () => {
  const range = computeRenderRange(1000, 720, 0, MARGIN_ROWS, WEEK_INDEX_MIN, WEEK_INDEX_MAX);
  assert.deepEqual(range, { start: WEEK_INDEX_MIN, end: WEEK_INDEX_MIN });
});

test("sameRange is true only for an identical start and end", () => {
  assert.equal(sameRange({ start: 1, end: 5 }, { start: 1, end: 5 }), true);
  assert.equal(sameRange({ start: 1, end: 5 }, { start: 1, end: 6 }), false);
  assert.equal(sameRange({ start: 1, end: 5 }, { start: 2, end: 5 }), false);
});

test("monthsForRenderRange spans exactly the months the range's days touch", () => {
  const start = dateToWeekIndex(new Date(2026, 0, 26)); // late Jan, week crosses into Feb
  const range = { start, end: start };
  const months = monthsForRenderRange(range);
  assert.deepEqual(months, ["2026-01", "2026-02"]);
});

test("withMonthMargin adds exactly one month before and after", () => {
  assert.deepEqual(withMonthMargin(["2026-06"]), ["2026-05", "2026-06", "2026-07"]);
  assert.deepEqual(withMonthMargin(["2026-01"]), ["2025-12", "2026-01", "2026-02"]);
  assert.deepEqual(withMonthMargin(["2026-12"]), ["2026-11", "2026-12", "2027-01"]);
});

test("withMonthMargin on an empty list stays empty", () => {
  assert.deepEqual(withMonthMargin([]), []);
});

test("computeFetchWindow composes the render months with margin", () => {
  const start = dateToWeekIndex(new Date(2026, 5, 15));
  const window = computeFetchWindow({ start, end: start });
  assert.deepEqual(window, ["2026-05", "2026-06", "2026-07"]);
});

test("sameMonthSet ignores order but not membership", () => {
  const committed = new Set(["2026-05", "2026-06", "2026-07"]);
  assert.equal(sameMonthSet(["2026-07", "2026-05", "2026-06"], committed), true);
  assert.equal(sameMonthSet(["2026-05", "2026-06"], committed), false);
  assert.equal(sameMonthSet(["2026-05", "2026-06", "2026-08"], committed), false);
});
