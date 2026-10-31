"use client";

import { useCallback, useLayoutEffect, useRef } from "react";
import {
  type TimelineAnchor,
  scrollTopForAnchor,
  timelineCenterTop,
  topAnchor,
} from "@/lib/order-timeline";
import { formatCalendarDate } from "@/lib/order-dates";
import type { OrderListItem } from "@/types/api";

/** Height of the today line between the two halves of the timeline. */
export const TIMELINE_DIVIDER_HEIGHT = 40;

/** How long the centring on today keeps being re-asserted while the layout
 * settles, unless the reader's first gesture ends it sooner. */
const HOLD_MS = 1500;

/** The line between what is ahead (above) and what is past (below). */
export function TodayDivider({ today }: { today: string }) {
  return (
    <div
      role="separator"
      aria-label="Today"
      data-testid="orders-today-line"
      style={{ height: TIMELINE_DIVIDER_HEIGHT }}
      className="flex items-center gap-3 border-b px-4 text-primary"
    >
      <span className="h-px flex-1 bg-primary/60" />
      <span className="text-[11px] font-semibold uppercase tracking-wide">
        Today · {formatCalendarDate(today, new Date())}
      </span>
      <span className="h-px flex-1 bg-primary/60" />
    </div>
  );
}

interface TimelineScrollArgs {
  /** The timeline is the view being shown. */
  active: boolean;
  /** The list's scroll container. */
  scrollEl: HTMLDivElement | null;
  items: Pick<OrderListItem, "id" | "timeline_date">[];
  splitIndex: number;
  rowHeight: number;
  /** Rows are in (or known to be empty): centring has something to go on. */
  ready: boolean;
  /** Everything that makes this a different timeline (filters, text): a
   * new one starts centred on today again. */
  resetKey: string;
}

/**
 * Keeps the timeline where it should be.
 *
 * Opening it, every change of the filters and `jumpToToday` centre the today
 * line -- arithmetic, since every row has one fixed height -- and hold that
 * while the layout settles (the viewport can still change size), until the
 * first gesture or a short timeout. After that the reader owns the position: an
 * entry arriving above them moves nothing they can see, because the row at
 * the viewport's top edge is remembered by id and offset and scrollTop is
 * corrected by exactly the delta that row moved. All writes go through
 * `write`, the one place scrollTop is assigned.
 */
export function useTimelineScroll({
  active,
  scrollEl,
  items,
  splitIndex,
  rowHeight,
  ready,
  resetKey,
}: TimelineScrollArgs) {
  const holdingRef = useRef(false);
  const holdTimerRef = useRef<number | null>(null);
  const anchorRef = useRef<TimelineAnchor | null>(null);
  const latestRef = useRef({ items, splitIndex });
  latestRef.current = { items, splitIndex };

  const geometry = useCallback(
    (split: number) => ({ rowHeight, dividerHeight: TIMELINE_DIVIDER_HEIGHT, splitIndex: split }),
    [rowHeight],
  );

  const write = useCallback((el: HTMLElement, top: number) => {
    el.scrollTop = top;
  }, []);

  const recordAnchor = useCallback(
    (el: HTMLElement) => {
      const { items: current, splitIndex: split } = latestRef.current;
      anchorRef.current = topAnchor(current, el.scrollTop, geometry(split));
    },
    [geometry],
  );

  const release = useCallback(() => {
    if (!holdingRef.current) return;
    holdingRef.current = false;
    if (holdTimerRef.current !== null) {
      window.clearTimeout(holdTimerRef.current);
      holdTimerRef.current = null;
    }
    if (scrollEl) recordAnchor(scrollEl);
  }, [scrollEl, recordAnchor]);

  const centre = useCallback(
    (el: HTMLElement) => {
      const { items: current, splitIndex: split } = latestRef.current;
      write(
        el,
        timelineCenterTop({
          splitIndex: split,
          itemCount: current.length,
          rowHeight,
          dividerHeight: TIMELINE_DIVIDER_HEIGHT,
          viewportHeight: el.clientHeight,
        }),
      );
    },
    [rowHeight, write],
  );

  const hold = useCallback(() => {
    holdingRef.current = true;
    anchorRef.current = null;
    if (holdTimerRef.current !== null) {
      window.clearTimeout(holdTimerRef.current);
      holdTimerRef.current = null;
    }
  }, []);

  // A new timeline: hold the centre.
  useLayoutEffect(() => {
    if (active) hold();
  }, [active, resetKey, hold]);

  const jumpToToday = useCallback(() => {
    if (!scrollEl) return;
    hold();
    centre(scrollEl);
    holdTimerRef.current = window.setTimeout(release, HOLD_MS);
  }, [scrollEl, hold, centre, release]);

  // Place the position on every change of the rows or the viewport.
  useLayoutEffect(() => {
    if (!active || !scrollEl || !ready) return;
    if (holdingRef.current) {
      centre(scrollEl);
      if (holdTimerRef.current === null) {
        holdTimerRef.current = window.setTimeout(release, HOLD_MS);
      }
      return;
    }
    const anchor = anchorRef.current;
    if (!anchor) return;
    const target = scrollTopForAnchor(items, anchor, geometry(splitIndex));
    if (target !== null && Math.abs(target - scrollEl.scrollTop) >= 1) write(scrollEl, target);
    recordAnchor(scrollEl);
  }, [active, scrollEl, ready, items, splitIndex, resetKey, centre, write, release, geometry, recordAnchor]);

  // The viewport changing size (a phone's address bar, rotation) while the
  // hold is on re-centres; the reader's gestures end the hold; scrolling
  // keeps the anchor current.
  useLayoutEffect(() => {
    if (!active || !scrollEl) return;
    const onResize = () => {
      if (holdingRef.current && ready) centre(scrollEl);
    };
    const onScroll = () => {
      if (!holdingRef.current) recordAnchor(scrollEl);
    };
    const observer = new ResizeObserver(onResize);
    observer.observe(scrollEl);
    const gestures = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    for (const type of gestures) scrollEl.addEventListener(type, release, { passive: true });
    scrollEl.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      observer.disconnect();
      for (const type of gestures) scrollEl.removeEventListener(type, release);
      scrollEl.removeEventListener("scroll", onScroll);
    };
  }, [active, scrollEl, ready, centre, release, recordAnchor]);

  // Leaving the timeline drops the hold and its timer.
  useLayoutEffect(
    () => () => {
      holdingRef.current = false;
      if (holdTimerRef.current !== null) window.clearTimeout(holdTimerRef.current);
    },
    [],
  );

  return { release, jumpToToday };
}
