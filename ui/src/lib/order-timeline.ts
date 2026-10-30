/**
 * The tickets timeline: entries newest-event first, a "today" line between
 * what is still ahead (above) and what is past (below). Rows have a fixed
 * height, so every position below is arithmetic rather than a measurement
 * -- centring on the line and keeping the reader's row still while entries
 * arrive above them need no layout reads. Pure; see order-timeline.test.ts.
 */

import type { OrderListItem } from "@/types/api";

type Dated = Pick<OrderListItem, "id" | "timeline_date">;

export type TimelineRow<T extends Dated> = { type: "order"; order: T } | { type: "today" };

export interface TimelineGeometry {
  rowHeight: number;
  dividerHeight: number;
  /** Rows above the today line. */
  splitIndex: number;
}

/** The local calendar date, YYYY-MM-DD -- the format `timeline_date` uses. */
export function todayString(now: Date): string {
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

/** How many leading entries sit above the line. An entry dated today is
 * still ahead (a date carries no time of day, and today's ticket is the one
 * about to be used); one without a date counts as past. Relies on the list
 * being sorted by `timeline_date` descending, nulls last, which the server's
 * `sort=event` guarantees. */
export function timelineSplitIndex(items: Dated[], today: string): number {
  let index = 0;
  while (index < items.length) {
    const date = items[index].timeline_date;
    if (date === null || date < today) break;
    index += 1;
  }
  return index;
}

export function buildTimelineRows<T extends Dated>(items: T[], today: string): TimelineRow<T>[] {
  const split = timelineSplitIndex(items, today);
  const rows: TimelineRow<T>[] = items.slice(0, split).map((order) => ({ type: "order", order }));
  rows.push({ type: "today" });
  for (const order of items.slice(split)) rows.push({ type: "order", order });
  return rows;
}

/** `scrollTop` that puts the middle of the today line in the middle of the
 * viewport, a whole pixel and inside the scrollable range. */
export function timelineCenterTop(args: {
  splitIndex: number;
  itemCount: number;
  rowHeight: number;
  dividerHeight: number;
  viewportHeight: number;
}): number {
  const { splitIndex, itemCount, rowHeight, dividerHeight, viewportHeight } = args;
  const wanted = Math.round(splitIndex * rowHeight + dividerHeight / 2 - viewportHeight / 2);
  const content = itemCount * rowHeight + dividerHeight;
  const max = Math.max(0, content - viewportHeight);
  return Math.max(0, Math.min(wanted, max));
}

function rowTop(index: number, geometry: TimelineGeometry): number {
  const { rowHeight, dividerHeight, splitIndex } = geometry;
  return index * rowHeight + (index >= splitIndex ? dividerHeight : 0);
}

export interface TimelineAnchor {
  id: string;
  /** How far the viewport's top edge sits below the row's top edge. */
  offset: number;
}

/** The first row reaching below the viewport's top edge. */
export function topAnchor(
  items: Dated[],
  scrollTop: number,
  geometry: TimelineGeometry,
): TimelineAnchor | null {
  for (let index = 0; index < items.length; index += 1) {
    const top = rowTop(index, geometry);
    if (top + geometry.rowHeight > scrollTop) return { id: items[index].id, offset: scrollTop - top };
  }
  return null;
}

/** Where `scrollTop` must be for the anchored row to sit where it did --
 * null when that row is no longer listed. */
export function scrollTopForAnchor(
  items: Dated[],
  anchor: TimelineAnchor,
  geometry: TimelineGeometry,
): number | null {
  const index = items.findIndex((item) => item.id === anchor.id);
  if (index < 0) return null;
  return rowTop(index, geometry) + anchor.offset;
}
