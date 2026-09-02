"use client";

/**
 * The continuous month view: a finite, huge, index-addressable list of
 * uniform-height week rows, absolutely positioned over one spacer. See the
 * design notes in the branch's commit history for why every row has the
 * same height and why this is hand-rolled rather than a virtualization
 * library -- in short, a whole-list resize needs a compensation this
 * component alone controls, and `scrollTop` needs exactly one writer.
 *
 * Position is identity, never pixels: `calendarDateAtom` holds the anchor
 * date, and `scrollToWeek` is the only function that ever writes
 * `scrollTop`. The scroll listener writes the week at the top back into the
 * atom, comparing against `currentWeekRef` so its own programmatic writes
 * never re-trigger a second scroll.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useAtom } from "jotai";
import { calendarDateAtom } from "@/lib/atoms";
import {
  WEEK_INDEX_MAX,
  WEEK_INDEX_MIN,
  dateToWeekIndex,
  format,
  weekDays,
  weekIndexToDate,
} from "@/lib/dates";
import { MonthWeekRow } from "@/components/calendar/month-week-row";
import { WEEK_NUMBER_GUTTER_WIDTH, type SelectEventHandler } from "@/components/calendar/layout";

const ROWS_PER_SCREEN_DESKTOP = 6;
const ROWS_PER_SCREEN_COMPACT = 8;
const MIN_ROW_HEIGHT = 72;
const MAX_ROW_HEIGHT = 180;
/** ~1.5 screens of margin each side of the visible range. */
const RENDER_MARGIN_ROWS = 8;

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

interface MonthScrollerProps {
  /** Phone shape: smaller rows, dots instead of chips, no spanning bars. */
  compact?: boolean;
  onSelectEvent: SelectEventHandler;
  onSelectDay: (date: Date) => void;
  /** A week number was clicked -- opens the week view on that week. */
  onSelectWeek: (date: Date) => void;
}

export function MonthScroller({ compact = false, onSelectEvent, onSelectDay, onSelectWeek }: MonthScrollerProps) {
  const [calendarDate, setCalendarDate] = useAtom(calendarDateAtom);
  const containerRef = useRef<HTMLDivElement>(null);

  const [rowHeight, setRowHeight] = useState(MIN_ROW_HEIGHT);
  const [viewportHeight, setViewportHeight] = useState(0);
  const [scrollTop, setScrollTop] = useState(0);
  const [monthLabel, setMonthLabel] = useState("");

  const rowHeightRef = useRef(rowHeight);
  rowHeightRef.current = rowHeight;

  const currentWeekRef = useRef<number>(dateToWeekIndex(calendarDate));
  const mountedRef = useRef(false);
  /** Set before a rowHeight change lands (initial mount, or a resize), so
   * the effect that applies it knows which week to restore and how far
   * through that row the reader was -- an absolute value computed from a
   * pre-mutation snapshot, never an increment. */
  const pendingScrollRef = useRef<{ week: number; fraction: number } | null>({
    week: currentWeekRef.current,
    fraction: 0,
  });

  const totalHeight = (WEEK_INDEX_MAX - WEEK_INDEX_MIN + 1) * rowHeight;

  const updateMonthLabel = useCallback((top: number, height: number) => {
    if (height <= 0) return;
    const headerWeek = Math.floor((top + 1.5 * height) / height) + WEEK_INDEX_MIN;
    const clamped = Math.min(WEEK_INDEX_MAX, Math.max(WEEK_INDEX_MIN, headerWeek));
    const thursday = weekDays(clamped)[3];
    setMonthLabel(format(thursday, "MMMM yyyy"));
  }, []);

  // Measure the viewport and derive rowHeight from it -- never estimated,
  // always computed, so every row is exactly the height the viewport
  // implies. A ResizeObserver (not just `window.resize`) so a sidebar
  // toggle or panel resize is caught too.
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const rowsPerScreen = compact ? ROWS_PER_SCREEN_COMPACT : ROWS_PER_SCREEN_DESKTOP;

    function applyMeasurement() {
      const h = container!.clientHeight;
      setViewportHeight(h);
      const next = Math.min(MAX_ROW_HEIGHT, Math.max(MIN_ROW_HEIGHT, Math.floor(h / rowsPerScreen) || MIN_ROW_HEIGHT));
      setRowHeight(next);
    }

    applyMeasurement();

    const observer = new ResizeObserver(() => {
      const el = containerRef.current;
      if (!el) return;
      const prevRowHeight = rowHeightRef.current;
      const week = currentWeekRef.current;
      const rowTop = (week - WEEK_INDEX_MIN) * prevRowHeight;
      const fraction = prevRowHeight > 0 ? (el.scrollTop - rowTop) / prevRowHeight : 0;
      pendingScrollRef.current = { week, fraction };
      applyMeasurement();
    });
    observer.observe(container);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compact]);

  // Apply whatever mount/resize left pending, now that rowHeight has landed
  // at the value it will be written against.
  useLayoutEffect(() => {
    const container = containerRef.current;
    const pending = pendingScrollRef.current;
    if (!container || !pending) return;
    const top = (pending.week - WEEK_INDEX_MIN) * rowHeight + pending.fraction * rowHeight;
    container.scrollTop = top;
    pendingScrollRef.current = null;
    setScrollTop(top);
    updateMonthLabel(top, rowHeight);
    mountedRef.current = true;
  }, [rowHeight, updateMonthLabel]);

  const scrollToWeek = useCallback(
    (week: number, behavior: ScrollBehavior) => {
      currentWeekRef.current = week;
      const container = containerRef.current;
      if (!container) return;
      const top = (week - WEEK_INDEX_MIN) * rowHeightRef.current;
      container.scrollTo({ top, behavior });
    },
    [],
  );

  // External navigation (Today, the mini-month, the toolbar arrows) writes
  // calendarDateAtom; this is the one place that turns that into a scroll.
  useEffect(() => {
    const week = dateToWeekIndex(calendarDate);
    if (week === currentWeekRef.current) return;
    scrollToWeek(week, mountedRef.current ? "smooth" : "instant");
  }, [calendarDate, scrollToWeek]);

  const handleScroll = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const top = container.scrollTop;
    setScrollTop(top);
    updateMonthLabel(top, rowHeightRef.current);

    const week = Math.floor(top / rowHeightRef.current) + WEEK_INDEX_MIN;
    if (week !== currentWeekRef.current) {
      currentWeekRef.current = week;
      setCalendarDate(weekIndexToDate(week));
    }
  }, [setCalendarDate, updateMonthLabel]);

  const firstVisible = Math.floor(scrollTop / rowHeight) + WEEK_INDEX_MIN;
  const lastVisible = Math.floor((scrollTop + viewportHeight) / rowHeight) + WEEK_INDEX_MIN;
  const renderStart = Math.max(WEEK_INDEX_MIN, firstVisible - RENDER_MARGIN_ROWS);
  const renderEnd = Math.min(WEEK_INDEX_MAX, lastVisible + RENDER_MARGIN_ROWS);
  const renderedWeeks: number[] = [];
  for (let w = renderStart; w <= renderEnd; w++) renderedWeeks.push(w);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {!compact && (
        <div className="flex items-center justify-between border-b px-3 py-1.5">
          <span className="text-sm font-medium">{monthLabel}</span>
        </div>
      )}
      <div className="flex border-b bg-muted/20 text-xs text-muted-foreground">
        {!compact && <div style={{ width: WEEK_NUMBER_GUTTER_WIDTH }} className="shrink-0" />}
        {WEEKDAY_LABELS.map((label) => (
          <div key={label} className="flex-1 px-1 py-1 text-center">
            {label}
          </div>
        ))}
      </div>
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="min-h-0 flex-1 overflow-y-auto"
        style={{ overflowAnchor: "none" }}
      >
        <div className="relative" style={{ height: totalHeight }}>
          {renderedWeeks.map((w) => (
            <div
              key={w}
              className="absolute inset-x-0"
              style={{ top: (w - WEEK_INDEX_MIN) * rowHeight, height: rowHeight }}
            >
              <MonthWeekRow
                weekIndex={w}
                rowHeight={rowHeight}
                compact={compact}
                onSelectEvent={onSelectEvent}
                onSelectDay={onSelectDay}
                onSelectWeek={onSelectWeek}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
