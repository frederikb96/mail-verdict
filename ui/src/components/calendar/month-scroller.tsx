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
 * date. `scrollToWeek` writes `scrollTop` for external navigation (Today,
 * the mini-month, the toolbar arrows); `applyMeasurement` writes it for a
 * mount or a resize, correcting for whatever rowHeight just became. The
 * scroll listener writes the week at the top back into the atom, comparing
 * against `currentWeekRef` so its own programmatic writes never re-trigger
 * a second scroll.
 *
 * Two more things are deliberately NOT driven by the raw scroll position:
 *
 * - React state changes only when the rendered week RANGE actually moves,
 *   never per pixel -- `renderRange` is derived from `scrollTop` on every
 *   scroll event, but only committed via `setState` when it differs from
 *   what's already rendered (a functional update returning the previous
 *   object when nothing changed, so React bails out with no re-render at
 *   all). `MonthWeekRow` is memoized on top of that, so even the rare
 *   range-changing commit only re-renders the rows that actually entered
 *   or left, never the whole grid.
 * - The URL and the *fetch* window (which months are actually requested)
 *   only catch up once scrolling **settles** -- a fixed quiet period with
 *   no scroll event, reset on every one. This is deliberately NOT the
 *   native `scrollend` event: it fires after every discrete wheel tick,
 *   not only when scrolling truly stops (measured -- a rapid series of
 *   plain wheel ticks each got their own `scrollend`, which turned every
 *   tick into a full URL write and cascaded into an app-wide re-render,
 *   far worse than the per-pixel state churn this file exists to avoid).
 *   The same timer also fixes the unrelated Safari bug the settle
 *   mechanism used to have: `programmaticScrollRef` was previously cleared
 *   only by `onScrollEnd`, which Safari never fires at all, so it stayed
 *   stuck true forever after any Today/mini-month jump there. A fast flick
 *   over years of months already answers instantly from cache and must
 *   not fire a request per row passed; the jotai atom, by contrast, is
 *   written on every row crossed, cheaply, so the toolbar and mini-month
 *   track the scroll live.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAtomValue, useSetAtom } from "jotai";
import { useCalendarUrlWriter } from "@/hooks/use-calendar-navigate";
import { useKeepEventChunksWarm } from "@/hooks/use-events";
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
import {
  type RenderRange,
  computeFetchWindow,
  computeRenderRange,
  sameMonthSet,
  sameRange,
} from "@/components/calendar/month-window";
import { WEEK_NUMBER_GUTTER_WIDTH, type SelectEventHandler } from "@/components/calendar/layout";

const ROWS_PER_SCREEN_DESKTOP = 6;
const ROWS_PER_SCREEN_COMPACT = 8;
const MIN_ROW_HEIGHT = 72;
const MAX_ROW_HEIGHT = 180;
/** ~1.5 screens of margin each side of the visible range. */
const RENDER_MARGIN_ROWS = 8;
/** How long scrolling has to be quiet before it's "settled": the URL and
 * the fetch window both catch up at this point, not on every scroll
 * event. Short enough that a genuine pause feels immediate, long enough
 * that a fast flick's intermediate rows never register as a pause. */
const SCROLL_SETTLE_MS = 200;

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const INITIAL_RENDER_RANGE = computeRenderRange(
  0, 0, MIN_ROW_HEIGHT, RENDER_MARGIN_ROWS, WEEK_INDEX_MIN, WEEK_INDEX_MAX,
);

interface MonthScrollerProps {
  /** Phone shape: smaller rows, dots instead of chips, no spanning bars. */
  compact?: boolean;
  onSelectEvent: SelectEventHandler;
  onSelectDay: (date: Date) => void;
  /** A week number was clicked -- opens the week view on that week. */
  onSelectWeek: (date: Date) => void;
}

export function MonthScroller({ compact = false, onSelectEvent, onSelectDay, onSelectWeek }: MonthScrollerProps) {
  const calendarDate = useAtomValue(calendarDateAtom);
  const setCalendarDate = useSetAtom(calendarDateAtom);
  const writeUrl = useCalendarUrlWriter();
  const containerRef = useRef<HTMLDivElement>(null);

  const [rowHeight, setRowHeight] = useState(MIN_ROW_HEIGHT);
  const [monthLabel, setMonthLabel] = useState("");
  const [renderRange, setRenderRange] = useState<RenderRange>(INITIAL_RENDER_RANGE);
  /** The months actually requested from the server -- see the file header
   * for why this lags `renderRange` until scrolling settles. */
  const [committedMonths, setCommittedMonths] = useState<ReadonlySet<string>>(() => new Set());
  const committedMonthsList = useMemo(() => Array.from(committedMonths), [committedMonths]);
  useKeepEventChunksWarm(committedMonthsList);

  const rowHeightRef = useRef(rowHeight);
  rowHeightRef.current = rowHeight;
  const viewportHeightRef = useRef(0);
  const scrollTopRef = useRef(0);
  const renderRangeRef = useRef(renderRange);
  renderRangeRef.current = renderRange;

  const currentWeekRef = useRef<number>(dateToWeekIndex(calendarDate));
  const mountedRef = useRef(false);
  /** True from the moment `scrollToWeek` issues a programmatic scroll until
   * it settles. While true, `handleScroll` still tracks the viewport for
   * rendering (the header label, which rows to render) but does not write
   * `calendarDateAtom` back -- the anchor `scrollToWeek` is animating
   * towards (e.g. today's own weekday) is already correct, and the scroll
   * events fired mid-animation only ever see the top row passing underneath
   * it, which is not the same date. */
  const programmaticScrollRef = useRef(false);
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

  /** Commits a new fetch window, but only replaces `committedMonths` when
   * its content actually differs -- so a settle that lands back where an
   * earlier one already committed bails out like any other unchanged
   * state, rather than forcing every warmed query to re-diff. */
  const commitFetchWindow = useCallback((range: RenderRange) => {
    const months = computeFetchWindow(range);
    setCommittedMonths((prev) => (sameMonthSet(months, prev) ? prev : new Set(months)));
  }, []);

  // `settle` (below) needs the latest writeUrl/commitFetchWindow without
  // being recreated itself -- it's scheduled fresh via setTimeout on every
  // scroll event, and a stable identity means resetSettleTimer doesn't
  // have to change either (writeUrl's own identity changes whenever
  // calendarDateAtom does, which is exactly every row crossed).
  const writeUrlRef = useRef(writeUrl);
  writeUrlRef.current = writeUrl;
  const commitFetchWindowRef = useRef(commitFetchWindow);
  commitFetchWindowRef.current = commitFetchWindow;

  const settleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const settle = useCallback(() => {
    if (settleTimerRef.current) {
      clearTimeout(settleTimerRef.current);
      settleTimerRef.current = null;
    }
    // Fixes the Safari bug the file header describes: nothing here depends
    // on `scrollend`, so this flag is cleared uniformly on every browser
    // rather than staying stuck true forever after a programmatic jump.
    programmaticScrollRef.current = false;
    writeUrlRef.current();
    commitFetchWindowRef.current(renderRangeRef.current);
  }, []);

  const resetSettleTimer = useCallback(() => {
    if (settleTimerRef.current) clearTimeout(settleTimerRef.current);
    settleTimerRef.current = setTimeout(settle, SCROLL_SETTLE_MS);
  }, [settle]);

  useEffect(() => () => {
    if (settleTimerRef.current) clearTimeout(settleTimerRef.current);
  }, []);

  // Measure the viewport and derive rowHeight from it -- never estimated,
  // always computed, so every row is exactly the height the viewport
  // implies. A ResizeObserver (not just `window.resize`) so a sidebar
  // toggle or panel resize is caught too.
  //
  // Measuring and correcting scrollTop happen in the same synchronous call,
  // both against the value `applyMeasurement` just computed -- never against
  // the `rowHeight` state, which cannot reflect it until a later render.
  // React runs every layout effect for a commit against that commit's own
  // state, so a `setRowHeight` queued by the *first* layout effect is not
  // yet visible to a *second* one in the same commit; a scroll correction
  // that read `rowHeight` from its own effect's dependency landed one
  // render too early, against the old scale, and by the time the corrected
  // rowHeight actually rendered there was nothing left in pendingScrollRef
  // to correct it with -- the exact mismatch this component exists to
  // prevent, silently reintroduced by routing the correction through state.
  //
  // A mount or resize commits the fetch window immediately, unlike organic
  // scrolling -- it's a single discrete jump, not a stream of events that
  // needs settling, and the initial paint should already have real data
  // warm rather than waiting out SCROLL_SETTLE_MS for no reason.
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const rowsPerScreen = compact ? ROWS_PER_SCREEN_COMPACT : ROWS_PER_SCREEN_DESKTOP;

    // Snapshot which week is at the top and how far through it the reader
    // is, before rowHeight changes under them -- an absolute value computed
    // from a pre-mutation snapshot. Skipped on the true first mount only:
    // pendingScrollRef's own useRef initializer already holds the right
    // target then, and the container has not been positioned yet to
    // snapshot from.
    function snapshotPending() {
      const prevRowHeight = rowHeightRef.current;
      const week = currentWeekRef.current;
      const rowTop = (week - WEEK_INDEX_MIN) * prevRowHeight;
      const fraction = prevRowHeight > 0 ? (container!.scrollTop - rowTop) / prevRowHeight : 0;
      pendingScrollRef.current = { week, fraction };
    }

    function applyMeasurement() {
      const h = container!.clientHeight;
      viewportHeightRef.current = h;
      const next = Math.min(MAX_ROW_HEIGHT, Math.max(MIN_ROW_HEIGHT, Math.floor(h / rowsPerScreen) || MIN_ROW_HEIGHT));
      rowHeightRef.current = next;
      setRowHeight(next);

      const pending = pendingScrollRef.current;
      if (!pending) return;
      const top = (pending.week - WEEK_INDEX_MIN) * next + pending.fraction * next;
      container!.scrollTop = top;
      pendingScrollRef.current = null;
      scrollTopRef.current = top;
      updateMonthLabel(top, next);
      const range = computeRenderRange(top, h, next, RENDER_MARGIN_ROWS, WEEK_INDEX_MIN, WEEK_INDEX_MAX);
      setRenderRange((prev) => (sameRange(prev, range) ? prev : range));
      commitFetchWindow(range);
      mountedRef.current = true;
    }

    if (mountedRef.current) snapshotPending();
    applyMeasurement();

    const observer = new ResizeObserver(() => {
      const el = containerRef.current;
      if (!el) return;
      snapshotPending();
      applyMeasurement();
    });
    observer.observe(container);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compact, updateMonthLabel, commitFetchWindow]);

  const scrollToWeek = useCallback(
    (week: number, behavior: ScrollBehavior) => {
      currentWeekRef.current = week;
      const container = containerRef.current;
      if (!container) return;
      programmaticScrollRef.current = true;
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
    scrollTopRef.current = top;
    updateMonthLabel(top, rowHeightRef.current);

    const range = computeRenderRange(
      top, viewportHeightRef.current, rowHeightRef.current, RENDER_MARGIN_ROWS, WEEK_INDEX_MIN, WEEK_INDEX_MAX,
    );
    // The functional-update form is what makes this a no-op commit when
    // the range hasn't moved: returning the same object React already
    // holds is an Object.is match, so it bails out before re-rendering
    // anything -- the mechanism behind "zero commits for a wheel movement
    // that stays inside one row".
    setRenderRange((prev) => (sameRange(prev, range) ? prev : range));

    resetSettleTimer();

    if (programmaticScrollRef.current) return;

    const week = Math.floor(top / rowHeightRef.current) + WEEK_INDEX_MIN;
    if (week !== currentWeekRef.current) {
      currentWeekRef.current = week;
      // Cheap: writes only the jotai atom, so the toolbar and mini-month
      // follow scroll without paying for a Next.js navigation on every row
      // crossed. The URL itself catches up once, in `settle`.
      setCalendarDate(weekIndexToDate(week));
    }
  }, [updateMonthLabel, resetSettleTimer, setCalendarDate]);

  const renderedWeeks: number[] = [];
  for (let w = renderRange.start; w <= renderRange.end; w++) renderedWeeks.push(w);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {!compact && (
        <div className="flex items-center justify-between border-b px-3 py-1.5">
          <span className="text-sm font-medium" data-testid="month-grid-title">
            {monthLabel}
          </span>
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
        className="no-scrollbar min-h-0 flex-1 overflow-y-auto"
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
                committedMonths={committedMonths}
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
