/**
 * Whether a list row reads as unread. Grouped by conversation, a row stands
 * for its thread's newest message but counts every message in it, the same
 * way the folder's own unread count does -- so an older unread reply behind
 * a read newest one still makes the row unread.
 */
export function isRowUnread(row: { is_seen: boolean; unread_in_thread?: number }): boolean {
  return !row.is_seen || (row.unread_in_thread ?? 0) > 0;
}
