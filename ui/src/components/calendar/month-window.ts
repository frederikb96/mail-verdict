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
 */

import { monthsBetween, weekDays } from "@/lib/dates";

export interface RenderRange {
  start: number;
  end: number;
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
