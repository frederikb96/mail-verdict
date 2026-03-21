/** Date and size formatting utilities. */

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
