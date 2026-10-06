import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ANCHOR_FRACTION,
  anchorOffset,
  computeAnchorWeek,
  computeFetchWindow,
  computeRenderRange,
  monthsForRenderRange,
  positionAt,
  restorePosition,
  sameMonthSet,
  sameRange,
  scrollTopForAnchorWeek,
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

test("anchorOffset is a fixed fraction of the viewport, above the middle", () => {
  assert.equal(anchorOffset(720), Math.round(720 * ANCHOR_FRACTION));
  assert.ok(ANCHOR_FRACTION < 0.5, "the anchor must sit above the literal middle");
  assert.ok(ANCHOR_FRACTION > 0, "the anchor must not sit at the very top");
});

test("anchorOffset is always a whole pixel, for any viewport height", () => {
  // A fractional offset survives the JS round trip fine (see the other
  // scrollTopForAnchorWeek test), but the browser stores scrollTop as a
  // whole pixel -- a fractional offset written then read back loses its
  // fraction, and flooring at what should be an exact row boundary then
  // drops a whole row. Every height here would have produced a fractional
  // offset before rounding was added.
  for (const h of [700, 720, 734, 800, 812, 667, 900, 1000, 1080, 733, 811]) {
    assert.equal(anchorOffset(h), Math.round(anchorOffset(h)), `h=${h}`);
  }
});

test("computeAnchorWeek picks the row spanning the anchor line, not the top row", () => {
  const anchor = dateToWeekIndex(new Date(2026, 8, 9));
  // Six rows visible (720 / 120); scrolled so `anchor` is the top row.
  const scrollTop = (anchor - WEEK_INDEX_MIN) * ROW_HEIGHT;
  const week = computeAnchorWeek(scrollTop, 720, ROW_HEIGHT, WEEK_INDEX_MIN);
  // Anchor line sits at 0.4 * 720 = 288px down, i.e. row 2 (288 / 120 = 2.4).
  assert.equal(week, anchor + 2);
});

test("computeAnchorWeek never divides by a zero row height", () => {
  assert.equal(computeAnchorWeek(1000, 720, 0, WEEK_INDEX_MIN), WEEK_INDEX_MIN);
});

test("scrollTopForAnchorWeek is the exact inverse of computeAnchorWeek at a row boundary", () => {
  const anchor = dateToWeekIndex(new Date(2026, 8, 9));
  const scrollTop = scrollTopForAnchorWeek(anchor, 720, ROW_HEIGHT, WEEK_INDEX_MIN);
  assert.equal(computeAnchorWeek(scrollTop, 720, ROW_HEIGHT, WEEK_INDEX_MIN), anchor);
});

test("scrollTopForAnchorWeek survives the browser rounding scrollTop to a whole pixel", () => {
  // The regression this guards: a fractional anchorOffset meant the write
  // and the read-back could disagree by under a pixel, and flooring a
  // value meant to land exactly on a row boundary dropped a whole week --
  // reproduced here by rounding the written scrollTop the way a browser
  // does, before reading it back through computeAnchorWeek.
  const anchor = dateToWeekIndex(new Date(2026, 8, 9));
  for (const h of [700, 720, 734, 800, 812, 667, 900, 1000, 1080, 733, 811]) {
    const written = scrollTopForAnchorWeek(anchor, h, ROW_HEIGHT, WEEK_INDEX_MIN);
    const storedByBrowser = Math.round(written);
    assert.equal(
      computeAnchorWeek(storedByBrowser, h, ROW_HEIGHT, WEEK_INDEX_MIN), anchor, `h=${h}`,
    );
  }
});

test("scrollTopForAnchorWeek scales with viewport height, not just row height", () => {
  const anchor = dateToWeekIndex(new Date(2026, 8, 9));
  const compact = scrollTopForAnchorWeek(anchor, 480, ROW_HEIGHT, WEEK_INDEX_MIN);
  const wide = scrollTopForAnchorWeek(anchor, 1200, ROW_HEIGHT, WEEK_INDEX_MIN);
  // A taller viewport pushes the anchor line further down, so the row it
  // targets has to start further up (a smaller, more negative scrollTop).
  assert.ok(wide < compact);
});

test("positionAt round-trips through scrollTopForAnchorWeek at any fraction", () => {
  const week = dateToWeekIndex(new Date(2026, 9, 6));
  const top = scrollTopForAnchorWeek(week, 757, ROW_HEIGHT, WEEK_INDEX_MIN) + 37;
  const pos = positionAt(top, 757, ROW_HEIGHT, WEEK_INDEX_MIN);
  assert.equal(pos.week, week);
  assert.equal(pos.fraction, 37 / ROW_HEIGHT);
  const restoredTop = (pos.week - WEEK_INDEX_MIN) * ROW_HEIGHT + pos.fraction * ROW_HEIGHT - anchorOffset(757);
  assert.equal(restoredTop, top);
});

test("positionAt reports the weeks on screen, partly visible rows included", () => {
  const top = 10 * ROW_HEIGHT + 50;
  const pos = positionAt(top, 720, ROW_HEIGHT, WEEK_INDEX_MIN);
  assert.equal(pos.firstVisible, WEEK_INDEX_MIN + 10);
  assert.equal(pos.lastVisible, WEEK_INDEX_MIN + 16);
});

test("restorePosition keeps the exact position for a week that was on screen", () => {
  const saved = { week: 500, fraction: 0.3, firstVisible: 497, lastVisible: 503 };
  assert.deepEqual(restorePosition(saved, 497), { week: 500, fraction: 0.3 });
  assert.deepEqual(restorePosition(saved, 503), { week: 500, fraction: 0.3 });
});

test("restorePosition lands on the target week's row top for anything else", () => {
  const saved = { week: 500, fraction: 0.3, firstVisible: 497, lastVisible: 503 };
  assert.deepEqual(restorePosition(saved, 504), { week: 504, fraction: 0 });
  assert.deepEqual(restorePosition(null, 500), { week: 500, fraction: 0 });
});
