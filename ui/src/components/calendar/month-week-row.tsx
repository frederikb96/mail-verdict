"use client";

/**
 * One week row of the month scroller: 7 day cells plus the spanning-bar
 * lanes for all-day and multi-day events. Every row is exactly `rowHeight`
 * tall regardless of content -- see month-scroller.tsx for why that is what
 * makes the scroller safe.
 *
 * Memoized: the month scroller only changes this row's own props (weekIndex,
 * rowHeight, compact, the calendars lookup, the three stable callbacks) when
 * something about THIS row actually changed. Without the memo, every
 * mounted row would still re-execute its body -- including its own
 * useWeekEvents call -- on every parent re-render, which is most of what
 * made the old version cost 1,000+ renders per wheel tick.
 */

import { memo, useMemo, useRef, useState } from "react";
import { useAtomValue } from "jotai";
import { format, isSameDay, isToday, isWeekend, weekDays, weekNumber } from "@/lib/dates";
import {
  allDayInstant,
  assignLanes,
  WEEK_NUMBER_GUTTER_WIDTH,
  type SelectEventHandler,
  type SpanningItem,
} from "@/components/calendar/layout";
import { EventChip } from "@/components/calendar/event-chip";
import { DayEventsPopover } from "@/components/calendar/day-events-popover";
import { useWeekEvents } from "@/hooks/use-events";
import { selectedEventAtom } from "@/lib/atoms";
import { cn } from "@/lib/utils";
import type { Calendar, EventInstance } from "@/types/api";

const DAY_HEADER_HEIGHT = 20;
const LANE_HEIGHT = 20;

interface SpanningInstance extends SpanningItem {
  event: EventInstance;
}

interface MonthWeekRowProps {
  weekIndex: number;
  rowHeight: number;
  compact: boolean;
  /** True while the scroller is flinging. A row that mounts light renders
   * its dates and no events -- the cost of a row full of chips is what
   * makes a fast fling over cached months stutter -- and fills them in on
   * the next render with `light` false, which the scroller issues once
   * scrolling settles. A row that has ever shown its events keeps them
   * whatever `light` says, so reading at any speed never sees a chip
   * disappear. */
  light: boolean;
  /** The calendars by id and the sidebar's hidden set, both built once by
   * the scroller from a single calendars subscription -- a row subscribing
   * on its own would re-render on every fetch of that query. */
  calendarById: ReadonlyMap<string, Calendar>;
  hiddenCalendarIds: ReadonlySet<string>;
  onSelectEvent: SelectEventHandler;
  onSelectDay: (date: Date) => void;
  onSelectWeek: (date: Date) => void;
}

function MonthWeekRowImpl({
  weekIndex,
  rowHeight,
  compact,
  light,
  calendarById,
  hiddenCalendarIds,
  onSelectEvent,
  onSelectDay,
  onSelectWeek,
}: MonthWeekRowProps) {
  const days = useMemo(() => weekDays(weekIndex), [weekIndex]);
  const { events, loaded: chunkLoaded } = useWeekEvents(weekIndex, hiddenCalendarIds);
  const shownEventsRef = useRef(false);
  const loaded = chunkLoaded && (!light || shownEventsRef.current);
  if (loaded) shownEventsRef.current = true;
  const selected = useAtomValue(selectedEventAtom);
  const [popoverDay, setPopoverDay] = useState<Date | null>(null);

  const weekStart = days[0];

  const { spanning, timedByDay } = useMemo(() => {
    const spanningItems: SpanningInstance[] = [];
    const timed: EventInstance[][] = Array.from({ length: 7 }, () => []);

    for (const e of events) {
      const start = new Date(e.dtstart);
      const end = new Date(e.dtend);
      const spansMultipleDays = !isSameDay(start, new Date(end.getTime() - 1));

      if (e.all_day || spansMultipleDays) {
        const spanStart = e.all_day ? allDayInstant(e.dtstart) : start;
        const spanEnd = e.all_day ? allDayInstant(e.dtend) : end;
        const startCol = Math.max(0, dayDiff(weekStart, spanStart));
        const endCol = Math.min(6, dayDiff(weekStart, new Date(spanEnd.getTime() - 1)));
        if (endCol < 0 || startCol > 6) continue;
        spanningItems.push({
          key: `${e.object_id}:${e.recurrence_id ?? "master"}`,
          startCol: Math.max(0, startCol),
          endCol: Math.max(0, endCol),
          event: e,
        });
      } else {
        for (let col = 0; col < 7; col++) {
          if (isSameDay(start, days[col])) {
            timed[col].push(e);
            break;
          }
        }
      }
    }

    for (const dayEvents of timed) {
      dayEvents.sort((a, b) => a.dtstart.localeCompare(b.dtstart));
    }

    return { spanning: spanningItems, timedByDay: timed };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, weekStart.getTime()]);

  const spanningLaned = assignLanes(spanning);
  const spanningLaneCount = spanningLaned.reduce((max, l) => Math.max(max, l.lane + 1), 0);

  const chipsPerCell = compact
    ? 0
    : Math.max(0, Math.floor((rowHeight - DAY_HEADER_HEIGHT) / LANE_HEIGHT));
  const visibleSpanningLanes = compact ? 0 : Math.min(spanningLaneCount, chipsPerCell);
  const timedCapacity = Math.max(0, chipsPerCell - visibleSpanningLanes);

  const hiddenCountForCol = (col: number): number => {
    const hiddenSpanning = spanningLaned.filter(
      (l) => l.lane >= visibleSpanningLanes && l.item.startCol <= col && col <= l.item.endCol,
    ).length;
    const hiddenTimed = Math.max(0, timedByDay[col].length - timedCapacity);
    return hiddenSpanning + hiddenTimed;
  };

  return (
    <div
      data-testid="week-row"
      data-week-index={weekIndex}
      className="relative flex border-b"
      style={{ height: rowHeight }}
    >
      {!compact && (
        <button
          type="button"
          data-testid="week-number"
          aria-label={`Open week of ${format(weekStart, "MMM d, yyyy")}`}
          onClick={() => onSelectWeek(weekStart)}
          style={{ width: WEEK_NUMBER_GUTTER_WIDTH }}
          className="shrink-0 border-r pt-0.5 text-center text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          {weekNumber(weekStart)}
        </button>
      )}
      {/* Its own relative container so the spanning-bar percentages below are
          computed against the 7-day area alone, not the row including the
          week-number gutter. */}
      <div className="relative flex min-w-0 flex-1">
        {days.map((day, col) => {
          const dayEvents = timedByDay[col].slice(0, compact ? 0 : timedCapacity);
          const hidden = hiddenCountForCol(col);
          const dotCount = compact
            ? spanningLaned.filter((l) => l.item.startCol <= col && col <= l.item.endCol).length +
              timedByDay[col].length
            : 0;

          // The cell is a plain element with a click handler, and the date
          // number is the control that opens the day: a cell that is itself
          // a <button> would nest the event chips' own buttons inside it,
          // which assistive technology cannot represent.
          return (
            <div
              key={isoKey(day)}
              data-date={isoKey(day)}
              onClick={() => onSelectDay(day)}
              className={cn(
                "relative flex min-w-0 flex-1 cursor-pointer flex-col overflow-hidden border-r px-1 pt-0.5 text-left last:border-r-0 hover:bg-accent/50",
                // Alternating by month (not by "current" month -- there is no
                // such thing in a continuous scroll) is what makes a month
                // boundary read as a colour change mid-row.
                day.getMonth() % 2 === 0 ? "bg-background" : "bg-muted/40",
              )}
            >
              <div className="flex h-5 items-center gap-1">
                <button
                  type="button"
                  aria-label={`Open ${format(day, "EEEE, MMM d, yyyy")}`}
                  onClick={(ev) => {
                    ev.stopPropagation();
                    onSelectDay(day);
                  }}
                  className={cn(
                    "flex h-5 min-w-5 items-center justify-center rounded-full px-1 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    isToday(day) && "bg-primary font-medium text-primary-foreground",
                    !isToday(day) && isWeekend(day) && "text-muted-foreground",
                  )}
                >
                  {day.getDate() === 1 ? format(day, "MMM d") : day.getDate()}
                </button>
              </div>

              {!compact && (
                <div
                  style={{ marginTop: visibleSpanningLanes * LANE_HEIGHT }}
                  className="flex flex-col gap-0.5"
                >
                  {!loaded ? (
                    // A chunk that has never loaded shows this instead of its
                    // events -- inside the same fixed-height cell, so a row
                    // whose data arrives later never changes height. Once
                    // `loaded` has been true, `placeholderData` keeps the
                    // previous chunk's events in place across a refetch, so
                    // this never reappears just because a refresh is in
                    // flight.
                    //
                    // Deliberately not `animate-pulse`: a fast flick keeps
                    // dozens of these mounted at once (fetching is deferred
                    // until scrolling settles, on purpose), and a CSS
                    // animation running on that many elements simultaneously
                    // cost more main-thread time than everything else this
                    // file fixed combined -- measured, p99 frame time went
                    // from ~150ms back down to ~17ms by dropping it. A
                    // static placeholder still shows loading without paying
                    // for continuous repaint.
                    <div className="h-3 w-4/5 rounded bg-muted/70" />
                  ) : (
                    <>
                      {dayEvents.map((e) => (
                        <EventChip
                          key={`${e.object_id}:${e.recurrence_id ?? "master"}`}
                          event={e}
                          calendar={calendarById.get(e.calendar_id)}
                          variant="month"
                          timeLabel={e.all_day ? undefined : format(new Date(e.dtstart), "HH:mm")}
                          selected={selected?.objectId === e.object_id}
                          onClick={(ev) => {
                            ev.stopPropagation();
                            onSelectEvent(e.object_id, e.recurrence_id, ev);
                          }}
                        />
                      ))}
                      {hidden > 0 && (
                        <span
                          role="button"
                          tabIndex={0}
                          onClick={(ev) => {
                            ev.stopPropagation();
                            setPopoverDay(day);
                          }}
                          className="w-fit cursor-pointer px-1 text-[11px] text-muted-foreground hover:text-foreground"
                        >
                          +{hidden} more
                        </span>
                      )}
                    </>
                  )}
                </div>
              )}

              {compact && !loaded && (
                <div className="mt-1 flex justify-center">
                  <div className="h-1 w-6 rounded-full bg-muted/70" />
                </div>
              )}

              {compact && loaded && dotCount > 0 && (
                <div className="mt-1 flex flex-wrap justify-center gap-0.5">
                  {Array.from({ length: Math.min(dotCount, 4) }).map((_, i) => (
                    <span key={i} className="h-1 w-1 rounded-full bg-[var(--cal-color,var(--muted-foreground))]" />
                  ))}
                </div>
              )}
            </div>
          );
        })}

        {!compact && visibleSpanningLanes > 0 && (
          <div
            className="pointer-events-none absolute inset-x-0"
            style={{ top: DAY_HEADER_HEIGHT }}
          >
            {spanningLaned
              .filter((l) => l.lane < visibleSpanningLanes)
              .map(({ item, lane }) => (
                <div
                  key={item.key}
                  className="pointer-events-auto absolute px-0.5"
                  style={{
                    left: `${(item.startCol / 7) * 100}%`,
                    width: `${((item.endCol - item.startCol + 1) / 7) * 100}%`,
                    top: lane * LANE_HEIGHT,
                  }}
                >
                  <EventChip
                    event={item.event}
                    calendar={calendarById.get(item.event.calendar_id)}
                    variant="allday"
                    selected={selected?.objectId === item.event.object_id}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      onSelectEvent(item.event.object_id, item.event.recurrence_id, ev);
                    }}
                  />
                </div>
              ))}
          </div>
        )}
      </div>

      {popoverDay && (
        <DayEventsPopover
          date={popoverDay}
          events={[
            ...spanning
              .filter((s) => {
                const col = dayDiff(weekStart, popoverDay);
                return s.startCol <= col && col <= s.endCol;
              })
              .map((s) => s.event),
            ...timedByDay[dayDiff(weekStart, popoverDay)],
          ]}
          onSelectEvent={onSelectEvent}
          onClose={() => setPopoverDay(null)}
        />
      )}
    </div>
  );
}

export const MonthWeekRow = memo(MonthWeekRowImpl);

function dayDiff(from: Date, to: Date): number {
  const a = new Date(from.getFullYear(), from.getMonth(), from.getDate());
  const b = new Date(to.getFullYear(), to.getMonth(), to.getDate());
  return Math.round((b.getTime() - a.getTime()) / 86_400_000);
}

function isoKey(date: Date): string {
  return format(date, "yyyy-MM-dd");
}
