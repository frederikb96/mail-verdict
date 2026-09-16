/**
 * The pure merge+filter behind useEventsForRange/useWeekEvents in
 * use-events.ts -- kept here, with no react-query or React import, so it
 * can be unit tested directly against plain data rather than only through
 * a mounted hook and a query client.
 */

import type { EventInstance, EventListResponse } from "@/types/api";

/** A stable key for one instance within a chunk -- a modified occurrence of
 * a recurring series shares its object_id with the master, so recurrence_id
 * has to be part of the identity. */
export function eventInstanceKey(e: Pick<EventInstance, "object_id" | "recurrence_id">): string {
  return `${e.object_id}:${e.recurrence_id ?? "master"}`;
}

/** Every instance touching [fromMs, toMs] (inclusive both ends), merged
 * across every chunk in `results` and de-duplicated by instance identity.
 * `results` takes only the one field a react-query result this needs, so
 * a caller (a test, or the hooks in use-events.ts) can pass plain objects
 * without mounting a query client. */
export function mergeEventsInRange(
  results: readonly { data?: Pick<EventListResponse, "events"> | undefined }[],
  fromMs: number,
  toMs: number,
): EventInstance[] {
  const byKey = new Map<string, EventInstance>();
  for (const r of results) {
    for (const e of r.data?.events ?? []) {
      byKey.set(eventInstanceKey(e), e);
    }
  }
  return Array.from(byKey.values()).filter((e) => {
    const start = new Date(e.dtstart).getTime();
    const end = new Date(e.dtend).getTime();
    return end >= fromMs && start <= toMs;
  });
}
