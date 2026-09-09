/**
 * The calendar's own address: `/calendar?view=<mode>&date=<yyyy-mm-dd>`.
 * Query parameters rather than path segments for the reason mail-url.ts
 * gives. One function builds it and its inverse sits beside it, so every
 * writer produces the identical string and use-calendar-url-sync.ts can
 * recognise that string when the router hands it back.
 */

import type { CalendarViewMode } from "@/lib/atoms";
import { isoDate } from "@/lib/dates";

export function calendarUrl(view: CalendarViewMode, date: Date): string {
  const params = new URLSearchParams({ view, date: isoDate(date) });
  return `/calendar?${params.toString()}`;
}

/** The `date` parameter read back: local midnight of that day, or null for
 * anything that is not a `yyyy-mm-dd`. */
export function parseCalendarDate(value: string | null): Date | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const parsed = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
