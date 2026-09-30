/**
 * Pure geometry for the month scroller: no React, no DOM, unit testable on
 * plain numbers. Two windows, kept deliberately separate:
 *
 * - The **render window** is what gets mounted -- computed straight from
 *   `scrollTop`, generous margin each side, updated live so scrolling never
 *   shows a gap.
 * - The **fetch window** is what gets requested from the server -- the
 *   months the render window overlaps, plus one month's buffer each side.
 *   It is deliberately computed as a separate step so the caller can commit
 *   it on a different cadence (once the reader has settled, not on every
 *   scroll event): a fast flick must not fire one request per row passed.
 *
 * A third, separate notion: the **anchor** -- the one row that decides both
 * the header month and which week is "current" (the top-left date, the
 * toolbar, the mini-month). It sits at a fixed fraction of the viewport
 * height down from the top, not at the top itself, so a week's neighbours on
 * both sides stay in view -- and expressed as a fraction of the actual
 * viewport rather than a fixed number of rows, it lands in the same relative
 * place whether few rows are visible (a phone) or many (a wide desktop
 * window).
 */

import { monthsBetween, weekDays } from "@/lib/dates";

export interface RenderRange {
  start: number;
  end: number;
}

/** How far down the viewport the anchor row sits, as a fraction of its
 * height -- a little above the middle, so the weeks before and after it are
 * both in view. */
export const ANCHOR_FRACTION = 0.4;

/** Pixel distance from the top of the viewport to the anchor line -- rounded
 * to a whole pixel, because the browser stores `scrollTop` as one: writing a
 * fractional offset and reading it back through `computeAnchorWeek` loses
 * the fraction, and flooring a value meant to land exactly on a row boundary
 * then drops to the row before it. An integer offset makes every round trip
 * exact regardless of what the browser does with `scrollTop`. */
export function anchorOffset(viewportHeight: number): number {
  return Math.round(viewportHeight * ANCHOR_FRACTION);
}

/** The week index whose row currently spans the anchor line. */
export function computeAnchorWeek(
  scrollTop: number,
  viewportHeight: number,
  rowHeight: number,
  min: number,
): number {
  if (rowHeight <= 0) return min;
  return Math.floor((scrollTop + anchorOffset(viewportHeight)) / rowHeight) + min;
}

/** The `scrollTop` that puts `week`'s row's top edge exactly on the anchor
 * line -- the inverse of `computeAnchorWeek`. */
export function scrollTopForAnchorWeek(
  week: number,
  viewportHeight: number,
  rowHeight: number,
  min: number,
): number {
  return (week - min) * rowHeight - anchorOffset(viewportHeight);
}

/** True when two ranges cover exactly the same weeks -- the caller's cue to
 * keep the previous object rather than replace it, so a `setState` bails
 * out instead of forcing a commit. */
export function sameRange(a: RenderRange, b: RenderRange): boolean {
  return a.start === b.start && a.end === b.end;
}

/** The inclusive week-index range to mount, `marginRows` beyond whatever is
 * actually visible on each side. */
export function computeRenderRange(
  scrollTop: number,
  viewportHeight: number,
  rowHeight: number,
  marginRows: number,
  min: number,
  max: number,
): RenderRange {
  if (rowHeight <= 0) return { start: min, end: min };
  const firstVisible = Math.floor(scrollTop / rowHeight) + min;
  const lastVisible = Math.floor((scrollTop + viewportHeight) / rowHeight) + min;
  return {
    start: Math.max(min, firstVisible - marginRows),
    end: Math.min(max, lastVisible + marginRows),
  };
}

/** Every month chunk key the render range's own days fall into. */
export function monthsForRenderRange(range: RenderRange): string[] {
  const startDate = weekDays(range.start)[0];
  const endDate = weekDays(range.end)[6];
  return monthsBetween(startDate, endDate);
}

function shiftMonthKey(month: string, deltaMonths: number): string {
  const [year, m] = month.split("-").map(Number);
  const d = new Date(year, m - 1 + deltaMonths, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

/** Adds one month's buffer before the first and after the last -- so a
 * reader who pauses right at a month boundary already has the next month
 * warm before they cross into it. */
export function withMonthMargin(months: string[]): string[] {
  if (months.length === 0) return months;
  return [shiftMonthKey(months[0], -1), ...months, shiftMonthKey(months[months.length - 1], 1)];
}

/** The fetch window for a given render range: every month it touches, plus
 * one month's margin on each side. */
export function computeFetchWindow(range: RenderRange): string[] {
  return withMonthMargin(monthsForRenderRange(range));
}

/** True when two fetch windows name exactly the same months, order aside --
 * the caller's cue to keep the previous Set rather than replace it. */
export function sameMonthSet(months: readonly string[], committed: ReadonlySet<string>): boolean {
  return months.length === committed.size && months.every((m) => committed.has(m));
}
