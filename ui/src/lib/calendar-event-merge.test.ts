import { test } from "node:test";
import assert from "node:assert/strict";
import { eventInstanceKey, mergeEventsInRange } from "./calendar-event-merge.ts";

function makeEvent(id: string, dtstart: string, dtend: string) {
  return {
    object_id: id,
    recurrence_id: null,
    calendar_id: "cal-1",
    dtstart,
    dtend,
  } as never;
}

test("mergeEventsInRange keeps only instances overlapping [fromMs, toMs]", () => {
  const results = [
    {
      data: {
        events: [
          makeEvent("in-range", "2026-06-10T10:00:00Z", "2026-06-10T11:00:00Z"),
          makeEvent("before", "2026-06-01T10:00:00Z", "2026-06-01T11:00:00Z"),
          makeEvent("after", "2026-06-30T10:00:00Z", "2026-06-30T11:00:00Z"),
        ],
      },
    },
  ];
  const from = new Date("2026-06-08T00:00:00Z").getTime();
  const to = new Date("2026-06-15T00:00:00Z").getTime();

  const events = mergeEventsInRange(results, from, to);

  assert.deepEqual(events.map((e) => e.object_id), ["in-range"]);
});

test("a call with the same chunk data but a DIFFERENT range returns different events -- the exact shape of the stale-memo bug", () => {
  // One month chunk holding two events on different days -- this is what
  // a week1 -> week2 Next click looks like when both weeks fall in the
  // same month: `results` (and its one entry's `data`) is unchanged, only
  // the range being filtered to moves. The bug this guards against
  // memoised on the data references alone and kept returning week1's
  // event for week2 as well.
  const results = [
    {
      data: {
        events: [
          makeEvent("week1-event", "2026-06-02T10:00:00Z", "2026-06-02T11:00:00Z"),
          makeEvent("week2-event", "2026-06-09T10:00:00Z", "2026-06-09T11:00:00Z"),
        ],
      },
    },
  ];

  const week1 = mergeEventsInRange(
    results,
    new Date("2026-06-01T00:00:00Z").getTime(),
    new Date("2026-06-08T00:00:00Z").getTime() - 1,
  );
  const week2 = mergeEventsInRange(
    results,
    new Date("2026-06-08T00:00:00Z").getTime(),
    new Date("2026-06-15T00:00:00Z").getTime() - 1,
  );

  assert.deepEqual(week1.map((e) => e.object_id), ["week1-event"]);
  assert.deepEqual(week2.map((e) => e.object_id), ["week2-event"]);
});

test("a results array whose length differs between calls is handled correctly -- the deps-array-length bug's own shape", () => {
  // A range touching one month, then a range touching two -- exactly what
  // `results` looks like stepping from a week entirely inside one month
  // to a week straddling into the next. Nothing here depends on how many
  // entries `results` has; only on which entries' events overlap the
  // range, which is the whole point of not keying a memo on the array's
  // own length.
  const septemberOnly = [
    { data: { events: [makeEvent("sep-event", "2026-09-15T10:00:00Z", "2026-09-15T11:00:00Z")] } },
  ];
  const septemberAndOctober = [
    { data: { events: [makeEvent("sep-event", "2026-09-15T10:00:00Z", "2026-09-15T11:00:00Z")] } },
    { data: { events: [makeEvent("oct-event", "2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z")] } },
  ];

  const fromMs = new Date("2026-09-28T00:00:00Z").getTime();
  const toMs = new Date("2026-10-05T00:00:00Z").getTime();

  assert.deepEqual(mergeEventsInRange(septemberOnly, fromMs, toMs), []);
  assert.deepEqual(
    mergeEventsInRange(septemberAndOctober, fromMs, toMs).map((e) => e.object_id),
    ["oct-event"],
  );
});

test("the same instance appearing in two chunks (a week spanning both) is de-duplicated by object_id + recurrence_id", () => {
  const shared = makeEvent("dup", "2026-06-08T23:00:00Z", "2026-06-09T01:00:00Z");
  const results = [{ data: { events: [shared] } }, { data: { events: [shared] } }];

  const events = mergeEventsInRange(
    results,
    new Date("2026-06-01T00:00:00Z").getTime(),
    new Date("2026-06-30T00:00:00Z").getTime(),
  );

  assert.equal(events.length, 1);
});

test("a chunk with no data yet (still loading) contributes nothing rather than throwing", () => {
  const results = [{ data: undefined }, { data: { events: [makeEvent("a", "2026-06-10T10:00:00Z", "2026-06-10T11:00:00Z")] } }];
  const events = mergeEventsInRange(
    results,
    new Date("2026-06-01T00:00:00Z").getTime(),
    new Date("2026-06-30T00:00:00Z").getTime(),
  );
  assert.deepEqual(events.map((e) => e.object_id), ["a"]);
});

test("eventInstanceKey distinguishes occurrences of the same series", () => {
  assert.notEqual(
    eventInstanceKey({ object_id: "series", recurrence_id: "20260601" }),
    eventInstanceKey({ object_id: "series", recurrence_id: "20260608" }),
  );
  assert.equal(
    eventInstanceKey({ object_id: "series", recurrence_id: null }),
    eventInstanceKey({ object_id: "series", recurrence_id: null }),
  );
});
