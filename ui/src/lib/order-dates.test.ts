import { test } from "node:test";
import assert from "node:assert/strict";
import { formatCalendarDate, formatEventRange } from "./order-dates.ts";

const now = new Date(2026, 9, 30, 12, 0);

test("a plain date renders as a local calendar day, whatever the zone", () => {
  assert.equal(formatCalendarDate("2026-10-31", now), "Sat 31 Oct");
});

test("the year is appended when it is not the current one", () => {
  assert.equal(formatCalendarDate("2027-01-04", now), "Mon 4 Jan 2027");
});

test("a range joins its ends, and one day stays one day", () => {
  assert.equal(formatEventRange("2026-11-02", "2026-11-05", now), "Mon 2 Nov – Thu 5 Nov");
  assert.equal(formatEventRange("2026-11-02", "2026-11-02", now), "Mon 2 Nov");
  assert.equal(formatEventRange("2026-11-02", null, now), "Mon 2 Nov");
  assert.equal(formatEventRange(null, null, now), "");
});

test("something that is not a date is returned untouched", () => {
  assert.equal(formatCalendarDate("next Tuesday", now), "next Tuesday");
});
