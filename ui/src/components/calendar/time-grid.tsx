"use client";

/** Day and week views: sticky headers, an all-day tray above the scroll
 * container (so its growth never moves the grid under the pointer), and
 * the hour-axis time grid itself. */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAtom, useAtomValue } from "jotai";
import { EventChip } from "@/components/calendar/event-chip";
import { EventEditor } from "@/components/calendar/event-editor";
import { RecurrenceScopeDialog } from "@/components/calendar/recurrence-scope-dialog";
import { TimeGridColumn } from "@/components/calendar/time-grid-column";
import {
  allDayInstant,
  assignLanes,
  type SelectEventHandler,
  type SpanningItem,
} from "@/components/calendar/layout";
import { useCalendars } from "@/hooks/use-calendars";
import { useDefaultCalendarId, useDefaultEventDurationMinutes } from "@/hooks/use-calendar-settings";
import { useEventsForRange, useUpdateEvent } from "@/hooks/use-events";
import { useGridDrag, type GridGhost } from "@/hooks/use-grid-drag";
import { useToast } from "@/hooks/use-toast";
import { calendarDateAtom, calendarScrollHourAtom, calendarZoomAtom } from "@/lib/atoms";
import { addDays, format, isSameDay, isToday, startOfWeek } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { EventInstance, RecurrenceScope } from "@/types/api";

/** HOUR_HEIGHT at zoom 1 -- the grid's actual row height is this times
 * the persisted zoom atom. */
const BASE_HOUR_HEIGHT = 56;
const MIN_ZOOM = 0.4;
const MAX_ZOOM = 3;
/** How much one wheel-delta unit under Ctrl moves the zoom -- tuned so a
 * normal trackpad/mouse-wheel notch (~100 raw delta) is a small, smooth
 * step rather than jumping the whole range in one scroll. */
const ZOOM_PER_WHEEL_DELTA = 0.0015;
/** Debounces the write into the persisted atom (which round-trips through
 * localStorage on every set) away from firing on every scroll frame; the
 * in-memory anchor ref, used for the zoom-recompute below, still updates
 * immediately regardless. */
const SCROLL_PERSIST_DEBOUNCE_MS = 300;

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
  const [zoom, setZoom] = useAtom(calendarZoomAtom);
  const [persistedScrollHour, setPersistedScrollHour] = useAtom(calendarScrollHourAtom);
  const HOUR_HEIGHT = BASE_HOUR_HEIGHT * zoom;
  const scrollRef = useRef<HTMLDivElement>(null);
  const { push: pushToast } = useToast();
  const { data: calendars } = useCalendars();
  const defaultDurationMinutes = useDefaultEventDurationMinutes();
  const defaultCalendarSetting = useDefaultCalendarId();
  const calendarById = useMemo(() => new Map((calendars ?? []).map((c) => [c.id, c])), [calendars]);
  // Same enabled/writable list and same default-calendar setting the
  // event editor itself honours -- click-to-create built its own,
  // independent "first calendar" pick here, which is exactly the B13
  // bug reached a second way: whichever calendar sorts first, not the
  // one actually chosen as the default.
  const writableCalendars = (calendars ?? []).filter((c) => !c.read_only && c.is_enabled);
  const validDefaultCalendarSetting = writableCalendars.some((c) => c.id === defaultCalendarSetting)
    ? defaultCalendarSetting
    : undefined;
  const updateEvent = useUpdateEvent();
  const [pendingScope, setPendingScope] = useState<{
    ghost: GridGhost;
    start: string;
    end: string;
    original: EventInstance | undefined;
  } | null>(null);
  const [createDefaults, setCreateDefaults] = useState<
    { start: Date; end: Date; calendarId: string } | null
  >(null);

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
        const spanStart = e.all_day ? allDayInstant(e.dtstart) : start;
        const spanEnd = e.all_day ? allDayInstant(e.dtend) : end;
        const startColIdx = days.findIndex((d) => isSameDay(d, spanStart) || spanStart < d);
        const reversedIdx = [...days]
          .reverse()
          .findIndex((d) => isSameDay(d, new Date(spanEnd.getTime() - 1)) || d < spanEnd);
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

  const commitMove = useCallback(
    (
      ghost: GridGhost,
      start: string,
      end: string,
      original: EventInstance | undefined,
      scope?: RecurrenceScope,
    ) => {
      const recurrenceFields = scope
        ? { scope, recurrence_id: scope === "this" ? (ghost.recurrenceId ?? undefined) : undefined }
        : {};
      updateEvent.mutate(
        {
          objectId: ghost.objectId,
          recurrenceId: ghost.recurrenceId,
          data: { dtstart: start, dtend: end, ...recurrenceFields },
        },
        {
          onSuccess: () => {
            pushToast(
              "Event moved",
              "success",
              6000,
              original
                ? {
                    label: "Undo",
                    onClick: () =>
                      updateEvent.mutate({
                        objectId: ghost.objectId,
                        recurrenceId: ghost.recurrenceId,
                        data: {
                          dtstart: original.dtstart,
                          dtend: original.dtend,
                          ...recurrenceFields,
                        },
                      }),
                  }
                : undefined,
            );
          },
          onError: (err) => pushToast(`Could not move event: ${err.message}`, "error", 0),
        },
      );
    },
    [updateEvent, pushToast],
  );

  const drag = useGridDrag({
    columns: days.length,
    pixelsPerMinute: HOUR_HEIGHT / 60,
    snapMinutes: defaultDurationMinutes,
    onCommitMove: (ghost) => {
      const day = days[ghost.column];
      const start = new Date(day);
      start.setHours(0, ghost.startMin, 0, 0);
      const end = new Date(day);
      end.setHours(0, ghost.endMin, 0, 0);
      const original = events.find(
        (e) => e.object_id === ghost.objectId && e.recurrence_id === ghost.recurrenceId,
      );
      // A drag never silently rewrites a whole series -- an occurrence of
      // one asks which occurrences the move applies to first.
      if (original?.is_recurring) {
        setPendingScope({ ghost, start: start.toISOString(), end: end.toISOString(), original });
        return;
      }
      commitMove(ghost, start.toISOString(), end.toISOString(), original);
    },
    onCommitCreate: (ghost) => {
      const day = days[ghost.column];
      const start = new Date(day);
      start.setHours(0, ghost.startMin, 0, 0);
      const end = new Date(day);
      end.setHours(0, ghost.endMin, 0, 0);
      const defaultCalendarId = validDefaultCalendarSetting ?? writableCalendars[0]?.id;
      if (!defaultCalendarId) {
        pushToast("Add a calendar before creating events", "warning");
        return;
      }
      // Nothing is created here -- the editor opens prefilled and the user
      // still has to press Save, same as the toolbar's New event button.
      setCreateDefaults({ start, end, calendarId: defaultCalendarId });
    },
  });

  // A chip that has just been dragged also receives the click the browser
  // derives from the same press-and-release: the drag captured the pointer
  // on it, which retargets that click back to it wherever the pointer
  // ended up. Opening the popover on it would show the values the move has
  // just replaced, so the derived click is dropped and a real one is not.
  const selectEventUnlessDragged: SelectEventHandler = useCallback(
    (objectId, recurrenceId, evt) => {
      if (drag.wasJustDragged(objectId, recurrenceId)) return;
      onSelectEvent(objectId, recurrenceId, evt);
    },
    [drag.wasJustDragged, onSelectEvent],
  );

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

  // The hour-of-day at the top of the viewport -- an identity to restore
  // and recompute against, per the scrolling skill, never a remembered
  // pixel offset (which a zoom change would make meaningless). Seeded
  // from whatever was persisted; still null the very first time this
  // view has ever been opened.
  const anchorHourRef = useRef<number | null>(persistedScrollHour);
  const persistTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Scroll to the persisted hour (or 8:00, or an hour before now on
  // today) once on mount.
  const scrolledRef = useRef(false);
  useEffect(() => {
    if (scrolledRef.current || !scrollRef.current) return;
    const showsToday = days.some((d) => isToday(d));
    const hour = anchorHourRef.current ?? (showsToday ? Math.max(0, new Date().getHours() - 1) : 8);
    anchorHourRef.current = hour;
    scrollRef.current.scrollTop = hour * HOUR_HEIGHT;
    scrolledRef.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A zoom change alone must not lose the reader's place: recompute
  // scrollTop from the anchor hour against whatever HOUR_HEIGHT now is,
  // rather than leaving the scrollTop a different scale produced.
  useLayoutEffect(() => {
    if (!scrolledRef.current || !scrollRef.current || anchorHourRef.current === null) return;
    scrollRef.current.scrollTop = anchorHourRef.current * HOUR_HEIGHT;
  }, [HOUR_HEIGHT]);

  const handleGridScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const hour = el.scrollTop / HOUR_HEIGHT;
    anchorHourRef.current = hour;
    if (persistTimeoutRef.current) clearTimeout(persistTimeoutRef.current);
    persistTimeoutRef.current = setTimeout(
      () => setPersistedScrollHour(hour), SCROLL_PERSIST_DEBOUNCE_MS,
    );
  }, [HOUR_HEIGHT, setPersistedScrollHour]);

  useEffect(() => {
    return () => {
      if (persistTimeoutRef.current) clearTimeout(persistTimeoutRef.current);
    };
  }, []);

  // Ctrl+wheel zooms the grid's vertical density. React's own onWheel is
  // a passive listener by default (attached at the root for scroll
  // perf), so preventDefault() inside it is silently ignored and the
  // page would also zoom natively underneath -- a real, non-passive
  // listener is the only way to actually stop that.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    function handleWheel(e: WheelEvent) {
      if (!e.ctrlKey) return;
      e.preventDefault();
      setZoom((z) => {
        const next = z - e.deltaY * ZOOM_PER_WHEEL_DELTA;
        return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next));
      });
    }
    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, [setZoom]);

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
        data-testid="time-grid-scroll"
        className="min-h-0 flex-1 overflow-y-auto"
        style={{ overflowAnchor: "none" }}
        onScroll={handleGridScroll}
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
              onSelectEvent={selectEventUnlessDragged}
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

      {pendingScope && (
        <RecurrenceScopeDialog
          open
          onOpenChange={(open) => {
            if (!open) setPendingScope(null);
          }}
          onConfirm={(scope) => {
            commitMove(pendingScope.ghost, pendingScope.start, pendingScope.end, pendingScope.original, scope);
            setPendingScope(null);
          }}
        />
      )}

      <EventEditor
        open={createDefaults !== null}
        onOpenChange={(open) => {
          if (!open) setCreateDefaults(null);
        }}
        mode="create"
        defaultDate={createDefaults?.start}
        dragRange={createDefaults ?? undefined}
        defaultCalendarId={createDefaults?.calendarId}
      />
    </div>
  );
}
