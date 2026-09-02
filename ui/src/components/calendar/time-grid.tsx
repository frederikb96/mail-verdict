"use client";

/** Day and week views: sticky headers, an all-day tray above the scroll
 * container (so its growth never moves the grid under the pointer), and
 * the hour-axis time grid itself. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAtomValue } from "jotai";
import { EventChip } from "@/components/calendar/event-chip";
import { TimeGridColumn } from "@/components/calendar/time-grid-column";
import { assignLanes, type SelectEventHandler, type SpanningItem } from "@/components/calendar/layout";
import { useCalendars } from "@/hooks/use-calendars";
import { useCreateEvent, useEventsForRange, useUpdateEvent } from "@/hooks/use-events";
import { useGridDrag } from "@/hooks/use-grid-drag";
import { useToast } from "@/hooks/use-toast";
import { calendarDateAtom } from "@/lib/atoms";
import { addDays, format, isSameDay, isToday, startOfWeek } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { EventInstance } from "@/types/api";

const HOUR_HEIGHT = 56;

interface TimeGridProps {
  /** 1 for the day view, 7 for the week view. */
  dayCount: 1 | 7;
  onSelectEvent: SelectEventHandler;
}

interface AllDaySpanning extends SpanningItem {
  event: EventInstance;
}

export function TimeGrid({ dayCount, onSelectEvent }: TimeGridProps) {
  const anchor = useAtomValue(calendarDateAtom);
  const scrollRef = useRef<HTMLDivElement>(null);
  const { push: pushToast } = useToast();
  const { data: calendars } = useCalendars();
  const calendarById = useMemo(() => new Map((calendars ?? []).map((c) => [c.id, c])), [calendars]);
  const updateEvent = useUpdateEvent();
  const createEvent = useCreateEvent();

  const days = useMemo(() => {
    if (dayCount === 1) return [anchor];
    const start = startOfWeek(anchor, { weekStartsOn: 1 });
    return Array.from({ length: 7 }, (_, i) => addDays(start, i));
  }, [anchor, dayCount]);

  const rangeStart = days[0];
  const rangeEnd = new Date(days[days.length - 1].getTime() + 24 * 60 * 60 * 1000 - 1);
  const { events } = useEventsForRange(rangeStart, rangeEnd);

  const { allDay, timedByColumn } = useMemo(() => {
    const spanning: AllDaySpanning[] = [];
    const timed: EventInstance[][] = Array.from({ length: days.length }, () => []);

    for (const e of events) {
      const start = new Date(e.dtstart);
      const end = new Date(e.dtend);
      const spansMultipleDays = !isSameDay(start, new Date(end.getTime() - 1));

      if (e.all_day || spansMultipleDays) {
        const startColIdx = days.findIndex((d) => isSameDay(d, start) || start < d);
        const reversedIdx = [...days].reverse().findIndex((d) => isSameDay(d, new Date(end.getTime() - 1)) || d < end);
        const endColIdx = reversedIdx === -1 ? -1 : days.length - 1 - reversedIdx;
        if (startColIdx === -1 || endColIdx === -1 || endColIdx < startColIdx) continue;
        spanning.push({
          key: `${e.object_id}:${e.recurrence_id ?? "master"}`,
          startCol: startColIdx,
          endCol: endColIdx,
          event: e,
        });
      } else {
        const col = days.findIndex((d) => isSameDay(d, start));
        if (col !== -1) timed[col].push(e);
      }
    }

    return { allDay: assignLanes(spanning), timedByColumn: timed };
  }, [events, days]);

  const allDayLaneCount = allDay.reduce((max, l) => Math.max(max, l.lane + 1), 0);
  const [showAllDay, setShowAllDay] = useState(false);
  const displayedAllDayLanes = showAllDay ? allDayLaneCount : Math.min(allDayLaneCount, 3);

  const drag = useGridDrag({
    columns: days.length,
    pixelsPerMinute: HOUR_HEIGHT / 60,
    onCommitMove: (ghost) => {
      const day = days[ghost.column];
      const start = new Date(day);
      start.setHours(0, ghost.startMin, 0, 0);
      const end = new Date(day);
      end.setHours(0, ghost.endMin, 0, 0);
      updateEvent.mutate(
        {
          objectId: ghost.objectId,
          recurrenceId: ghost.recurrenceId,
          data: { dtstart: start.toISOString(), dtend: end.toISOString() },
        },
        {
          onSuccess: () => pushToast("Event moved", "success", 4000),
          onError: (err) => pushToast(`Could not move event: ${err.message}`, "error", 0),
        },
      );
    },
    onCommitCreate: (ghost) => {
      const day = days[ghost.column];
      const start = new Date(day);
      start.setHours(0, ghost.startMin, 0, 0);
      const end = new Date(day);
      end.setHours(0, ghost.endMin, 0, 0);
      const defaultCalendar = calendars?.find((c) => !c.read_only);
      if (!defaultCalendar) {
        pushToast("Add a calendar before creating events", "warning");
        return;
      }
      createEvent.mutate({
        calendar_id: defaultCalendar.id,
        summary: "",
        dtstart: start.toISOString(),
        dtend: end.toISOString(),
      });
    },
  });

  const handlePointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!drag.ghost) return;
      const surface = (e.target as HTMLElement).closest("[data-grid-row]") as HTMLElement | null;
      const rect = surface?.getBoundingClientRect();
      if (!rect) return;
      drag.updateDrag(rect, e.clientX, e.clientY);
    },
    [drag],
  );

  // Scroll to 8:00 (or an hour before now, on today) once on mount.
  const scrolledRef = useRef(false);
  useEffect(() => {
    if (scrolledRef.current || !scrollRef.current) return;
    const showsToday = days.some((d) => isToday(d));
    const hour = showsToday ? Math.max(0, new Date().getHours() - 1) : 8;
    scrollRef.current.scrollTop = hour * HOUR_HEIGHT;
    scrolledRef.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex border-b">
        <div className="w-14 shrink-0" />
        {days.map((day) => (
          <div key={day.toISOString()} className="flex flex-1 flex-col items-center border-l py-1">
            <span className="text-xs text-muted-foreground">{format(day, "EEE")}</span>
            <span
              className={cn(
                "flex h-6 w-6 items-center justify-center rounded-full text-sm",
                isToday(day) && "bg-primary font-medium text-primary-foreground",
              )}
            >
              {day.getDate()}
            </span>
          </div>
        ))}
      </div>

      {allDayLaneCount > 0 && (
        <div className="flex border-b">
          <div className="flex w-14 shrink-0 items-center justify-end pr-1 text-[10px] text-muted-foreground">
            All day
          </div>
          <div className="relative flex-1" style={{ height: displayedAllDayLanes * 20 + 4 }}>
            <div className="absolute inset-0 flex">
              {days.map((_, i) => (
                <div key={i} className="flex-1 border-l" />
              ))}
            </div>
            {allDay
              .filter((l) => l.lane < displayedAllDayLanes)
              .map(({ item, lane }) => (
                <div
                  key={item.key}
                  className="absolute px-0.5"
                  style={{
                    left: `${(item.startCol / days.length) * 100}%`,
                    width: `${((item.endCol - item.startCol + 1) / days.length) * 100}%`,
                    top: lane * 20,
                  }}
                >
                  <EventChip
                    event={item.event}
                    calendar={calendarById.get(item.event.calendar_id)}
                    variant="allday"
                    onClick={(ev) => {
                      ev.stopPropagation();
                      onSelectEvent(item.event.object_id, item.event.recurrence_id, ev);
                    }}
                  />
                </div>
              ))}
            {allDayLaneCount > 3 && (
              <button
                type="button"
                className="absolute right-1 top-0 text-[10px] text-muted-foreground hover:text-foreground"
                onClick={() => setShowAllDay(!showAllDay)}
              >
                {showAllDay ? "Show less" : `+${allDayLaneCount - 3} more`}
              </button>
            )}
          </div>
        </div>
      )}

      <div
        ref={scrollRef}
        className="min-h-0 flex-1 overflow-y-auto"
        style={{ overflowAnchor: "none" }}
        onPointerMove={handlePointerMove}
        onPointerUp={drag.commit}
        onPointerCancel={drag.cancel}
      >
        <div className="flex" data-grid-row>
          <div className="w-14 shrink-0">
            {Array.from({ length: 24 }).map((_, h) => (
              <div
                key={h}
                className="border-b pr-1 text-right text-[10px] text-muted-foreground"
                style={{ height: HOUR_HEIGHT }}
              >
                {h > 0 && `${String(h).padStart(2, "0")}:00`}
              </div>
            ))}
          </div>
          {days.map((day, col) => (
            <TimeGridColumn
              key={day.toISOString()}
              date={day}
              column={col}
              events={timedByColumn[col]}
              hourHeight={HOUR_HEIGHT}
              ghost={drag.ghost}
              onSelectEvent={onSelectEvent}
              onPointerDownMove={(e, event, startMin, endMin, kind) =>
                drag.beginMove(e, event.object_id, event.recurrence_id, startMin, endMin, col, kind)
              }
              onPointerDownCreate={(e) => {
                const row = (e.currentTarget as HTMLElement).closest("[data-grid-row]");
                const rect = row?.getBoundingClientRect();
                if (rect) drag.beginCreate(rect.top, e.clientY, col);
              }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
