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

/** The 6-week grid a month picker renders, Monday first, wide enough to
 * cover any month's own start-of-week overhang on either side. Shared by
 * the sidebar's MiniMonth and the event editor's date field so both read
 * the same days for the same anchor. */
export function daysOfMonthGrid(monthAnchor: Date): Date[] {
  const gridStart = startOfWeek(startOfMonth(monthAnchor), { weekStartsOn: 1 });
  const gridEnd = endOfWeek(
    new Date(monthAnchor.getFullYear(), monthAnchor.getMonth() + 1, 0), { weekStartsOn: 1 },
  );
  const days: Date[] = [];
  for (let d = gridStart; d <= gridEnd; d = addDays(d, 1)) days.push(d);
  return days;
}

/** The display format the event editor's date/time field renders and
 * parses -- day first, 24-hour, always, regardless of the browser's own
 * locale (deliberately: an English-locale browser with a German user is
 * exactly the mismatch this control exists to fix). Defined once and
 * imported everywhere that needs it, rather than restated as a literal
 * format string at each call site. */
export const DATE_TIME_DISPLAY_FORMAT = "dd.MM.yyyy HH:mm";
export const DATE_DISPLAY_FORMAT = "dd.MM.yyyy";

const pad2 = (n: number) => String(n).padStart(2, "0");
/** A year always occupies four digits in a date/datetime-local value --
 * an unpadded one is not a value the native control can parse, so it
 * silently empties itself instead, which is what turns a half-typed year
 * into an unreadable field. */
const padYear = (n: number) => String(n).padStart(4, "0");

/** The native `datetime-local` value (`YYYY-MM-DDTHH:mm`) for an instant,
 * in the browser's own local time. */
export function toLocalInputValue(iso: string): string {
  const d = new Date(iso);
  return `${padYear(d.getFullYear())}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** The wall clock an instant reads as in the browser's own zone, for a
 * request that sends `tz` alongside: the API keeps the reading it is
 * given and binds it to that zone, so sending a UTC instant there would
 * stamp the UTC reading onto the local zone -- an event entered at 10:00
 * stored as 10:00 in a zone it was never 10:00 in. */
export function toLocalWallClock(iso: string): string {
  return `${toLocalInputValue(iso)}:00`;
}

/** What a `datetime-local`/`date` value currently names, as an instant --
 * or null while it names nothing readable. Ordinary typing goes through
 * such states (retyping a year momentarily leaves the control empty). */
export function fromInputValue(value: string, allDay: boolean): string | null {
  const d = allDay ? wholeDayDate(value) : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/** The calendar day an instant falls on in the browser's own local time --
 * what someone looking at a wall-clock time means by "today", unlike the
 * UTC day the same instant can carry near midnight in a positive offset. */
export function toLocalDateValue(iso: string): string {
  const d = new Date(iso);
  return `${padYear(d.getFullYear())}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** An all-day dtstart/dtend carries no timezone at all (RFC 5545
 * VALUE=DATE) -- the API always encodes the day as literal UTC midnight,
 * so reading it back must read the UTC date directly rather than through
 * whatever zone the browser sits in, or the same stored day would render
 * differently depending on where it's opened. */
export function toWholeDayValue(iso: string): string {
  const d = new Date(iso);
  return `${padYear(d.getUTCFullYear())}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
}

export function wholeDayDate(value: string): Date {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

export function wholeDayIso(value: string): string {
  return wholeDayDate(value).toISOString();
}

export function addDaysIso(iso: string, days: number): string {
  const d = new Date(iso);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString();
}

export { isSameDay, isSameMonth, startOfMonth, startOfWeek, endOfWeek, addMonths, addDays, addWeeks, format };
