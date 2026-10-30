/** Calendar dates as the orders screens show them. The server sends plain
 * `YYYY-MM-DD` days; `new Date("2026-10-31")` would read one as UTC
 * midnight and show the day before in any zone west of Greenwich, so they
 * are parsed as local days. */

import { format, isSameYear, parseISO } from "date-fns";

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** "Sat 31 Oct", with the year appended when it is not the current one.
 * Text that is not a plain day comes back as it was. */
export function formatCalendarDate(value: string, now: Date): string {
  if (!DAY.test(value)) return value;
  const date = parseISO(value);
  if (Number.isNaN(date.getTime())) return value;
  return format(date, isSameYear(date, now) ? "EEE d MMM" : "EEE d MMM yyyy");
}

/** One day, or "start – end" for a stay or a multi-day event. */
export function formatEventRange(start: string | null, end: string | null, now: Date): string {
  if (!start) return end ? formatCalendarDate(end, now) : "";
  const first = formatCalendarDate(start, now);
  if (!end || end === start) return first;
  return `${first} – ${formatCalendarDate(end, now)}`;
}
