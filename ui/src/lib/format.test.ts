import { test } from "node:test";
import assert from "node:assert/strict";
import { formatFullDate, formatRecipientList, formatRelativeAgo, formatRelativeDate } from "./format.ts";

/** "Now" for every test below: a Thursday, well clear of midnight so the
 * day-boundary cases (today/yesterday) land unambiguously. */
const NOW = new Date(2026, 8, 3, 17, 35).getTime(); // Thu 3 Sep 2026, 17:35 local

function withFrozenClock(fn: () => void) {
  test.mock.timers.enable({ apis: ["Date"], now: NOW });
  try {
    fn();
  } finally {
    test.mock.timers.reset();
  }
}

test("formatRelativeDate: today reads as a bare 24-hour time", () => {
  withFrozenClock(() => {
    const earlierToday = new Date(2026, 8, 3, 9, 5).toISOString();
    assert.equal(formatRelativeDate(earlierToday), "09:05");
  });
});

test("formatRelativeDate: yesterday reads as the word, not a duration", () => {
  withFrozenClock(() => {
    const yesterday = new Date(2026, 8, 2, 22, 0).toISOString();
    assert.equal(formatRelativeDate(yesterday), "Yesterday");
  });
});

test("formatRelativeDate: within the last week reads as a weekday name", () => {
  withFrozenClock(() => {
    const fourDaysAgo = new Date(2026, 7, 30, 8, 0).toISOString(); // Sunday
    assert.equal(formatRelativeDate(fourDaysAgo), "Sun");
  });
});

test("formatRelativeDate: older than a week reads as a day-first date, same year", () => {
  withFrozenClock(() => {
    const threeWeeksAgo = new Date(2026, 7, 12, 8, 0).toISOString();
    assert.equal(formatRelativeDate(threeWeeksAgo), "12 Aug");
  });
});

test("formatRelativeDate: a previous year gets the year appended", () => {
  withFrozenClock(() => {
    const lastYear = new Date(2025, 11, 24, 8, 0).toISOString();
    assert.equal(formatRelativeDate(lastYear), "24 Dec 2025");
  });
});

test("formatRelativeDate: no date renders nothing", () => {
  assert.equal(formatRelativeDate(null), "");
});

test("formatRelativeAgo: a few minutes ago reads as a duration", () => {
  withFrozenClock(() => {
    const fiveMinAgo = new Date(NOW - 5 * 60_000).toISOString();
    assert.equal(formatRelativeAgo(fiveMinAgo), "5m ago");
  });
});

test("formatRelativeAgo: under a minute reads as 'just now'", () => {
  withFrozenClock(() => {
    assert.equal(formatRelativeAgo(new Date(NOW - 1000).toISOString()), "just now");
  });
});

test("formatRelativeAgo: an hour or more falls back to the plain list format", () => {
  withFrozenClock(() => {
    const yesterday = new Date(2026, 8, 2, 22, 0).toISOString();
    assert.equal(formatRelativeAgo(yesterday), "Yesterday");
  });
});

test("formatFullDate: day-first, 24-hour, with the weekday", () => {
  const iso = new Date(2026, 8, 3, 17, 35).toISOString();
  assert.equal(formatFullDate(iso), "Thu, 3 Sep 2026, 17:35");
});

test("formatRecipientList: full addresses, not display names", () => {
  const addrs = ['"Frederik Berg" <fberg@posteo.de>', "frederik.berg@posteo.net"];
  assert.equal(formatRecipientList(addrs), "fberg@posteo.de, frederik.berg@posteo.net");
});

test("formatRecipientList: truncates with a count tail past the max", () => {
  const addrs = ["a@x.com", "b@x.com", "c@x.com", "d@x.com"];
  assert.equal(formatRecipientList(addrs, 2), "a@x.com, b@x.com +2 more");
});

test("formatRecipientList: empty or missing renders nothing", () => {
  assert.equal(formatRecipientList(null), null);
  assert.equal(formatRecipientList([]), null);
});
