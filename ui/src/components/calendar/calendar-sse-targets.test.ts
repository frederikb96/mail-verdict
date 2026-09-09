import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveCalendarInvalidationTargets } from "./calendar-sse-targets.ts";

test("a recurring series invalidates everything", () => {
  const result = resolveCalendarInvalidationTargets(
    { id: "a", dtstart: "2026-06-01T10:00:00Z", dtend: "2026-06-01T11:00:00Z", is_recurring: true },
    [],
  );
  assert.equal(result, "all");
});

test("is_recurring null (older server, unknown) invalidates everything", () => {
  const result = resolveCalendarInvalidationTargets(
    { id: "a", dtstart: "2026-06-01T10:00:00Z", dtend: "2026-06-01T11:00:00Z", is_recurring: null },
    [],
  );
  assert.equal(result, "all");
});

test("missing is_recurring (older server) invalidates everything", () => {
  const result = resolveCalendarInvalidationTargets(
    { id: "a", dtstart: "2026-06-01T10:00:00Z", dtend: "2026-06-01T11:00:00Z" },
    [],
  );
  assert.equal(result, "all");
});

test("missing dtstart/dtend invalidates everything even when not recurring", () => {
  const result = resolveCalendarInvalidationTargets({ id: "a", is_recurring: false }, []);
  assert.equal(result, "all");
});

test("a plain single-day event targets only the month it falls in", () => {
  const result = resolveCalendarInvalidationTargets(
    { id: "a", dtstart: "2026-06-15T10:00:00Z", dtend: "2026-06-15T11:00:00Z", is_recurring: false },
    [],
  );
  assert.deepEqual(result, ["2026-06"]);
});

test("an event spanning a month boundary targets both months", () => {
  const result = resolveCalendarInvalidationTargets(
    { id: "a", dtstart: "2026-06-30T22:00:00Z", dtend: "2026-07-01T02:00:00Z", is_recurring: false },
    [],
  );
  assert.deepEqual(result, ["2026-06", "2026-07"]);
});

test("months the id was previously seen in are included, even away from the new dates", () => {
  // A move: the instance used to live in April, now lives in June -- the
  // April chunk still shows it (stale) until invalidated too.
  const result = resolveCalendarInvalidationTargets(
    { id: "a", dtstart: "2026-06-15T10:00:00Z", dtend: "2026-06-15T11:00:00Z", is_recurring: false },
    ["2026-04"],
  );
  assert.deepEqual(result, ["2026-04", "2026-06"]);
});

test("an absurdly wide span falls back to invalidating everything", () => {
  const result = resolveCalendarInvalidationTargets(
    { id: "a", dtstart: "2000-01-01T00:00:00Z", dtend: "2030-01-01T00:00:00Z", is_recurring: false },
    [],
  );
  assert.equal(result, "all");
});

test("an unparseable date falls back to invalidating everything", () => {
  const result = resolveCalendarInvalidationTargets(
    { id: "a", dtstart: "not-a-date", dtend: "2026-06-15T11:00:00Z", is_recurring: false },
    [],
  );
  assert.equal(result, "all");
});
