import { test } from "node:test";
import assert from "node:assert/strict";
import {
  SLIDER_WEEKS,
  formatDay,
  labelledTicks,
  monthTicks,
  parseDay,
  resolveWindow,
  sliderStopDate,
  sliderStops,
  sliderWindow,
  windowLabel,
} from "./search-window.ts";

/** Thu 3 Sep 2026, 17:35 local. */
const NOW = new Date(2026, 8, 3, 17, 35);

test("formatDay writes ISO order whatever the locale", () => {
  assert.equal(formatDay(new Date(2026, 0, 5)), "2026-01-05");
});

test("parseDay accepts real days only", () => {
  assert.equal(formatDay(parseDay("2026-02-28")!), "2026-02-28");
  assert.equal(parseDay("2026-02-31"), null);
  assert.equal(parseDay("05.01.2026"), null);
  assert.equal(parseDay("2026-1-5"), null);
});

test("presets resolve against the clock", () => {
  const today = resolveWindow({ kind: "preset", preset: "today" }, NOW);
  assert.equal(today.after, new Date(2026, 8, 3).toISOString());
  assert.equal(today.before, NOW.toISOString());

  const yesterday = resolveWindow({ kind: "preset", preset: "since-yesterday" }, NOW);
  assert.equal(yesterday.after, new Date(2026, 8, 2).toISOString());

  const week = resolveWindow({ kind: "preset", preset: "last-7-days" }, NOW);
  assert.equal(week.after, new Date(NOW.getTime() - 7 * 24 * 3_600_000).toISOString());

  const month = resolveWindow({ kind: "preset", preset: "last-30-days" }, NOW);
  assert.equal(month.after, new Date(NOW.getTime() - 30 * 24 * 3_600_000).toISOString());

  // Rolling: the same stored preset resolves differently a day later.
  const later = new Date(2026, 8, 4, 9, 0);
  assert.notEqual(resolveWindow({ kind: "preset", preset: "last-7-days" }, later).after, week.after);
});

test("slider stops are weekly from local midnight, 52 weeks back", () => {
  assert.equal(formatDay(sliderStopDate(SLIDER_WEEKS, NOW)), "2026-09-03");
  assert.equal(formatDay(sliderStopDate(SLIDER_WEEKS - 1, NOW)), "2026-08-27");
  assert.equal(formatDay(sliderStopDate(0, NOW)), "2025-09-04");
});

test("a slider selection ending at the last stop runs to now, otherwise to the end of that day", () => {
  const open = sliderWindow(10, SLIDER_WEEKS, NOW);
  assert.deepEqual(open, { kind: "slider", since: formatDay(sliderStopDate(10, NOW)), until: null });
  assert.equal(resolveWindow(open, NOW).before, NOW.toISOString());

  const closed = sliderWindow(10, 20, NOW);
  assert.equal(
    resolveWindow(closed, NOW).before,
    new Date(2026, 8, 3 - (SLIDER_WEEKS - 20) * 7, 23, 59, 59, 999).toISOString(),
  );
  assert.equal(resolveWindow(closed, NOW).after, sliderStopDate(10, NOW).toISOString());
});

test("slider stops round-trip, and a non-slider window opens the full axis", () => {
  assert.deepEqual(sliderStops(sliderWindow(7, 31, NOW), NOW), [7, 31]);
  assert.deepEqual(sliderStops(sliderWindow(7, SLIDER_WEEKS, NOW), NOW), [7, SLIDER_WEEKS]);
  assert.deepEqual(sliderStops(null, NOW), [0, SLIDER_WEEKS]);
  assert.deepEqual(sliderStops({ kind: "preset", preset: "today" }, NOW), [0, SLIDER_WEEKS]);
  assert.deepEqual(
    sliderStops({ kind: "custom", since: "2026-01-01", until: "2026-02-01" }, NOW),
    [0, SLIDER_WEEKS],
  );
});

test("a custom window covers whole days, both ends inclusive", () => {
  const range = resolveWindow({ kind: "custom", since: "2026-03-01", until: "2026-03-02" }, NOW);
  assert.equal(range.after, new Date(2026, 2, 1).toISOString());
  assert.equal(range.before, new Date(2026, 2, 2, 23, 59, 59, 999).toISOString());
});

test("labels", () => {
  assert.equal(windowLabel({ kind: "preset", preset: "last-7-days" }), "Last 7 days");
  assert.equal(
    windowLabel({ kind: "custom", since: "2026-03-01", until: "2026-03-02" }),
    "2026-03-01 – 2026-03-02",
  );
  assert.equal(windowLabel({ kind: "slider", since: "2026-03-01", until: null }), "2026-03-01 – now");
});

test("month ticks mark each month once", () => {
  const ticks = monthTicks(NOW);
  assert.ok(ticks.every((t, i) => i === 0 || t.stop > ticks[i - 1].stop));
  assert.ok(ticks.every((t) => t.stop >= 0 && t.stop <= SLIDER_WEEKS));
  assert.ok(ticks.length >= 12 && ticks.length <= 13);
  // January carries the year, every other month its short name.
  assert.deepEqual(
    ticks.filter((t) => t.isYear).map((t) => t.label),
    ["2026"],
  );
  assert.ok(ticks.some((t) => t.label === "Oct"));
});

test("labels that would touch are dropped, years first", () => {
  const ticks = monthTicks(NOW);
  const wide = labelledTicks(ticks, 700);
  assert.equal(wide.size, ticks.length);

  const narrow = labelledTicks(ticks, 320);
  const year = ticks.find((t) => t.isYear)!;
  assert.ok(narrow.has(year.stop));
  assert.ok(narrow.size < ticks.length);
  // No two shown labels overlap.
  const xs = ticks.filter((t) => narrow.has(t.stop)).map((t) => (t.stop / SLIDER_WEEKS) * 320);
  for (let i = 1; i < xs.length; i++) assert.ok(xs[i] - xs[i - 1] >= 30);
});
