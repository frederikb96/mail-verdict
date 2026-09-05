/** Date and size formatting utilities. */

import { format as formatDate } from "date-fns";

/**
 * Format a date string as a relative time (e.g., "2h ago", "Yesterday")
 * or absolute date for older items.
 */
export function formatRelativeDate(dateStr: string | null): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60_000);
  const diffHours = Math.floor(diffMs / 3_600_000);
  const diffDays = Math.floor(diffMs / 86_400_000);

  if (diffMin < 1) return "now";
  if (diffMin < 60) return `${diffMin}m`;
  if (diffHours < 24) return `${diffHours}h`;
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays}d`;

  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: date.getFullYear() !== now.getFullYear() ? "numeric" : undefined,
  });
}

/** formatRelativeDate() plus the trailing "ago" a sentence like "Synced
 * ... ago" needs -- only for the forms that read as a duration ("5m",
 * "2h", "3d"). "now", "Yesterday" and an absolute date already read fine
 * as a sentence's tail without it; appending "ago" to any of those reads
 * as "Synced now ago" / "Synced Yesterday ago". */
export function formatRelativeAgo(dateStr: string | null): string {
  const relative = formatRelativeDate(dateStr);
  if (relative === "now") return "just now";
  if (/^\d+[mhd]$/.test(relative)) return `${relative} ago`;
  return relative;
}

/** Format a full date for display in reading pane header. */
export function formatFullDate(dateStr: string | null): string {
  if (!dateStr) return "";
  return new Date(dateStr).toLocaleString(undefined, {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
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
