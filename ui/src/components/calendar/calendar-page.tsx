"use client";

import { useCallback } from "react";
import { useAtomValue, useSetAtom } from "jotai";
import { CalendarToolbar } from "@/components/calendar/calendar-toolbar";
import { MonthScroller } from "@/components/calendar/month-scroller";
import { TimeGrid } from "@/components/calendar/time-grid";
import { AgendaList } from "@/components/calendar/agenda-list";
import { EventPopover } from "@/components/calendar/event-popover";
import type { SelectEventHandler } from "@/components/calendar/layout";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  calendarDateAtom,
  calendarViewAtom,
  eventPopoverAnchorAtom,
  selectedEventAtom,
} from "@/lib/atoms";

export function CalendarPage() {
  const view = useAtomValue(calendarViewAtom);
  const setCalendarDate = useSetAtom(calendarDateAtom);
  const setSelectedEvent = useSetAtom(selectedEventAtom);
  const setAnchor = useSetAtom(eventPopoverAnchorAtom);
  const isMobile = useIsMobile();

  const handleSelectEvent: SelectEventHandler = useCallback(
    (objectId, recurrenceId, evt) => {
      setSelectedEvent({ objectId, recurrenceId });
      if (evt) {
        setAnchor((evt.currentTarget as HTMLElement).getBoundingClientRect());
      } else {
        setAnchor(null);
      }
    },
    [setSelectedEvent, setAnchor],
  );

  const handleSelectDay = useCallback(
    (date: Date) => {
      setCalendarDate(date);
    },
    [setCalendarDate],
  );

  const effectiveView = isMobile && view === "week" ? "day" : view;

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <CalendarToolbar />
      <div className="min-h-0 flex-1 overflow-hidden">
        {effectiveView === "month" && (
          <MonthScroller
            compact={isMobile}
            onSelectEvent={handleSelectEvent}
            onSelectDay={handleSelectDay}
          />
        )}
        {effectiveView === "week" && <TimeGrid dayCount={7} onSelectEvent={handleSelectEvent} />}
        {effectiveView === "day" && <TimeGrid dayCount={1} onSelectEvent={handleSelectEvent} />}
        {effectiveView === "agenda" && <AgendaList onSelectEvent={handleSelectEvent} />}
      </div>
      <EventPopover />
    </div>
  );
}
