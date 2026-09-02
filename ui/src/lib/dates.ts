/**
 * date-fns wrappers for the calendar's week/month arithmetic.
 *
 * The month scroller works in week indices rather than dates -- see
 * components/calendar/month-scroller.tsx for why. This module is the one
 * place that translates between the two.
 */

import {
  addDays,
  addMonths,
  addWeeks,
  differenceInCalendarWeeks,
  endOfWeek,
  format,
  getISOWeek,
  isSameDay,
  isSameMonth,
  isToday as dfIsToday,
  isWeekend as dfIsWeekend,
  startOfMonth,
  startOfWeek,
} from "date-fns";

/** Every week index is counted from this Monday. Chosen only as a stable
 * origin -- it carries no meaning beyond that. */
export const WEEK_EPOCH = startOfWeek(new Date(2000, 0, 3), { weekStartsOn: 1 });

/** The month scroller renders weeks in this closed range. ~3,700 weeks at a
 * 120px row is far under both browsers' element-size ceilings, so the whole
 * range renders as one absolutely-positioned spacer with no prepending. */
export const WEEK_INDEX_MIN = -weeksBetween(WEEK_EPOCH, new Date(1990, 0, 1));
export const WEEK_INDEX_MAX = weeksBetween(WEEK_EPOCH, new Date(2060, 11, 31));

function weeksBetween(from: Date, to: Date): number {
  return differenceInCalendarWeeks(to, from, { weekStartsOn: 1 });
}

/** The Monday-start week index containing `date`. */
export function dateToWeekIndex(date: Date): number {
  return differenceInCalendarWeeks(date, WEEK_EPOCH, { weekStartsOn: 1 });
}

/** The Monday of the given week index. */
export function weekIndexToDate(index: number): Date {
  return addWeeks(WEEK_EPOCH, index);
}

/** The 7 days of a week index, Monday first. */
export function weekDays(index: number): Date[] {
  const monday = weekIndexToDate(index);
  return Array.from({ length: 7 }, (_, i) => addDays(monday, i));
}

/** ISO date (no time) for a day cell's `data-date` attribute and API params. */
export function isoDate(date: Date): string {
  return format(date, "yyyy-MM-dd");
}

/** The `YYYY-MM` chunk key events are fetched and cached by. */
export function monthChunkKey(date: Date): string {
  return format(date, "yyyy-MM");
}

/** Every month chunk key a week's days can fall into (at most 2, at a month
 * boundary). */
export function monthChunksForWeek(index: number): string[] {
  const days = weekDays(index);
  return Array.from(new Set(days.map(monthChunkKey)));
}

/** ISO-8601 week number, for the gutter next to the month view. */
export function weekNumber(date: Date): number {
  return getISOWeek(date);
}

export function isToday(date: Date): boolean {
  return dfIsToday(date);
}

export function isWeekend(date: Date): boolean {
  return dfIsWeekend(date);
}

export { isSameDay, isSameMonth, startOfMonth, startOfWeek, endOfWeek, addMonths, addDays, addWeeks, format };
