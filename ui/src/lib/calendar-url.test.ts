import { test } from "node:test";
import assert from "node:assert/strict";
import { calendarUrl, parseCalendarDate } from "./calendar-url.ts";

test("calendarUrl writes the view and the local calendar day", () => {
  assert.equal(calendarUrl("month", new Date(2026, 3, 20, 23, 30)), "/calendar?view=month&date=2026-04-20");
});

test("parseCalendarDate is the inverse of calendarUrl's date parameter", () => {
  const day = new Date(2026, 11, 31);
  const url = new URL(calendarUrl("week", day), "http://x");
  const back = parseCalendarDate(url.searchParams.get("date"));
  assert.equal(back?.getTime(), day.getTime());
  assert.equal(calendarUrl("week", back!), calendarUrl("week", day));
});

test("parseCalendarDate rejects anything that is not a yyyy-mm-dd", () => {
  for (const bad of [null, "", "2026-4-20", "20260420", "2026-04-20T00:00", "not a date"]) {
    assert.equal(parseCalendarDate(bad), null, String(bad));
  }
});
