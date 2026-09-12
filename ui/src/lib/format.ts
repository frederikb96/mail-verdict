/** Date and size formatting utilities. */

import {
  differenceInCalendarDays,
  differenceInMinutes,
  format as formatDate,
  isToday as dfIsToday,
  isYesterday as dfIsYesterday,
} from "date-fns";

/**
 * The one date/time convention every timestamp in the app uses -- day
 * first, 24-hour, independent of the browser's own locale (the same
 * reasoning as the calendar's DATE_TIME_DISPLAY_FORMAT in lib/dates.ts:
 * an English-locale browser with a German user is exactly the mismatch
 * this exists to avoid). A mail list column reads relative-to-today
 * rather than as a plain date -- see formatRelativeDate below -- but
 * both resolve to this same day-month ordering and 24-hour clock.
 */

/**
 * A list row's timestamp: the time for anything received today, the
 * weekday name for the six days before that, and a day-first date
 * beyond it -- with the year appended only once it is not the current
 * one. Never a relative duration ("2h", "3d"): those drift as the clock
 * moves on without the row re-rendering, so a message read minutes ago
 * looks identical to one read yesterday until the next paint.
 *
 * A date after today is always the plain date. The timestamp comes from
 * the sender's own Date header, and spam routinely dates itself days
 * ahead to sit at the top of the list; a bare weekday would pass for one
 * in the past week.
 */
export function formatRelativeDate(dateStr: string | null): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  const now = new Date();

  if (dfIsToday(date)) return formatDate(date, "HH:mm");
  if (dfIsYesterday(date)) return "Yesterday";
  const daysAgo = differenceInCalendarDays(now, date);
  if (daysAgo > 0 && daysAgo < 7) return formatDate(date, "EEE");

  return formatDate(date, date.getFullYear() === now.getFullYear() ? "d MMM" : "d MMM yyyy");
}

/** formatRelativeDate() plus the trailing "ago" a sentence like "Synced
 * ... ago" needs -- only for a message received within the last hour,
 * the one case formatRelativeDate reads as a bare clock time that would
 * otherwise make no sense mid-sentence ("Synced 14:32"). Everything
 * older already reads fine as a sentence's tail without it. */
export function formatRelativeAgo(dateStr: string | null): string {
  if (!dateStr) return "";
  const minutesAgo = differenceInMinutes(new Date(), new Date(dateStr));
  if (minutesAgo < 1) return "just now";
  if (minutesAgo < 60) return `${minutesAgo}m ago`;
  return formatRelativeDate(dateStr);
}

/** The full date/time shown in the reading pane and thread header --
 * weekday, day-first date, 24-hour time, in the app's one convention
 * rather than the browser's locale. */
export function formatFullDate(dateStr: string | null): string {
  if (!dateStr) return "";
  return formatDate(new Date(dateStr), "EEE, d MMM yyyy, HH:mm");
}

/** Format file size in human-readable form. */
export function formatSize(bytes: number | null): string {
  if (bytes === null || bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

/** Extract sender display name from email address string. */
export function extractSenderName(from: string | null): string {
  if (!from) return "Unknown";
  // Handle "Name <email@example.com>" format
  const match = from.match(/^"?([^"<]+)"?\s*<.*>$/);
  if (match) return match[1].trim();
  // Handle plain email
  return from.split("@")[0];
}

/** Extract email address from sender string. */
export function extractEmail(from: string | null): string {
  if (!from) return "";
  const match = from.match(/<([^>]+)>/);
  return match ? match[1] : from;
}

/** Generate initials from a name (1-2 letters). */
export function getInitials(name: string): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

/** Format addresses for display (handles both string and array). */
export function formatAddresses(
  addrs: string | string[] | null,
): string {
  if (!addrs) return "";
  if (Array.isArray(addrs)) return addrs.join(", ");
  return addrs;
}

/** A recipient line for a search result row: the full address rather than
 * just its display name -- a name alone is ambiguous the moment two of
 * the reader's own addresses share it (several Posteo identities, say),
 * which a bare local part or display name cannot disambiguate. Joined and
 * truncated to a small, fixed count with a "+N more" tail rather than
 * growing the row for a message with a long recipient list. `null`/empty
 * -- no To at all, which a genuinely to-less message can have -- renders
 * nothing. */
export function formatRecipientList(addrs: string[] | null, maxShown = 3): string | null {
  if (!addrs || addrs.length === 0) return null;
  const emails = addrs.map((a) => extractEmail(a));
  if (emails.length <= maxShown) return emails.join(", ");
  return `${emails.slice(0, maxShown).join(", ")} +${emails.length - maxShown} more`;
}

/** Parse a comma/semicolon-separated address field into a list. This does
 * not validate the individual addresses -- a caller that turns free text
 * into recipients checks each one with `isValidEmail` before sending it
 * anywhere, since a send that never leaves reports its failure much later
 * than the field it was typed into. */
export function parseAddressList(value: string): string[] {
  return value
    .split(/[,;]/)
    .map((a) => a.trim())
    .filter(Boolean);
}

function isValidCalendarDate(date: Date, month: number, day: number, year?: number): boolean {
  if (Number.isNaN(date.getTime())) return false;
  // `new Date` silently rolls an out-of-range day/month forward (Feb 30
  // becomes Mar 2) instead of failing -- round-tripping the parts is what
  // catches that.
  return (
    date.getMonth() === month - 1 &&
    date.getDate() === day &&
    (year === undefined || date.getFullYear() === year)
  );
}

export interface ContactBirthdayParts {
  year: number | null;
  month: number;
  day: number;
}

/** A vCard BDAY value carries whatever shape the sending app chose to
 * write, verbatim -- a full `YYYY-MM-DD`, RFC 6350's year-less `--MM-DD` /
 * `--MMDD` (common: "we know the birthday, not the birth year"), or free
 * text. `new Date(raw)` throws on the year-less forms and on anything
 * date-fns' `format()` then can't format, which is the "invalid time
 * value" crash a malformed or partial birthday produced. Returns `null`
 * for whatever it cannot confidently parse. */
export function parseContactBirthday(raw: string): ContactBirthdayParts | null {
  const trimmed = raw.trim();

  let m = /^--(\d{2})-?(\d{2})$/.exec(trimmed);
  if (m) {
    const month = Number(m[1]);
    const day = Number(m[2]);
    const date = new Date(2000, month - 1, day);
    return isValidCalendarDate(date, month, day) ? { year: null, month, day } : null;
  }

  m = /^(\d{4})-?(\d{2})-?(\d{2})$/.exec(trimmed);
  if (m) {
    const year = Number(m[1]);
    const month = Number(m[2]);
    const day = Number(m[3]);
    const date = new Date(year, month - 1, day);
    return isValidCalendarDate(date, month, day, year) ? { year, month, day } : null;
  }

  return null;
}

/** For display: `null` for anything that cannot be confidently parsed, so
 * a caller can render nothing rather than either garbage or a crash. */
export function formatContactBirthday(raw: string): string | null {
  const parts = parseContactBirthday(raw);
  if (!parts) return null;
  const date = new Date(parts.year ?? 2000, parts.month - 1, parts.day);
  return formatDate(date, parts.year !== null ? "MMMM d, yyyy" : "MMMM d");
}

/** A permissive shape check -- one @ with something on each side, no
 * whitespace -- not full RFC 5322 validation. Good enough to catch a plain
 * word typed and committed by mistake before it reaches the outbox. */
export function isValidEmail(address: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(address);
}
