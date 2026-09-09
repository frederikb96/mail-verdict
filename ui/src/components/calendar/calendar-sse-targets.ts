/**
 * Pure targeting logic for `calendar.object` SSE events: which cached
 * month chunks a changed instance can actually affect, so a live update
 * invalidates only those rather than every mounted chunk. No React, no
 * DOM, no QueryClient -- the caller resolves "which chunks currently hold
 * this object" from the cache and passes the plain result in.
 */

import { monthsBetween } from "@/lib/dates";

/** The subset of the `calendar.object` SSE payload this decision needs. */
export interface CalendarObjectPayload {
  id?: string;
  dtstart?: string | null;
  dtend?: string | null;
  is_recurring?: boolean | null;
}

/** A span wider than this is treated the same as missing dates -- walking
 * every month of a corrupted or absurd payload is not worth doing, and the
 * broad invalidate is always safe. */
const MAX_SPAN_MONTHS = 60;

function monthsBetweenInstants(startIso: string, endIso: string): string[] | null {
  const start = new Date(startIso);
  const end = new Date(endIso);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null;
  const months = monthsBetween(start, end);
  return months.length > MAX_SPAN_MONTHS ? null : months;
}

/**
 * `"all"` means invalidate every loaded chunk -- the safe fallback for a
 * recurring series (any occurrence's own chunk membership can move), for a
 * payload missing the fields a targeted decision needs (an older server),
 * or for a date span too wide to be worth walking. Otherwise the exact
 * months to invalidate: every chunk `monthsContainingId` says currently
 * holds this object (so a chunk it just moved OUT of is caught too), union
 * the months its own [dtstart, dtend] falls into.
 */
export function resolveCalendarInvalidationTargets(
  payload: CalendarObjectPayload,
  monthsContainingId: readonly string[],
): "all" | string[] {
  if (payload.is_recurring !== false) return "all";
  if (!payload.dtstart || !payload.dtend) return "all";
  const dateMonths = monthsBetweenInstants(payload.dtstart, payload.dtend);
  if (dateMonths === null) return "all";
  return Array.from(new Set([...monthsContainingId, ...dateMonths]));
}
