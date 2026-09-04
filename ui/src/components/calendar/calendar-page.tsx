"use client";

import { useCallback } from "react";
import { useAtom, useSetAtom } from "jotai";
import { CalendarToolbar } from "@/components/calendar/calendar-toolbar";
import { MonthScroller } from "@/components/calendar/month-scroller";
import { TimeGrid } from "@/components/calendar/time-grid";
import { AgendaList } from "@/components/calendar/agenda-list";
import { EventPopover } from "@/components/calendar/event-popover";
import type { SelectEventHandler } from "@/components/calendar/layout";
import { useCalendarNavigate } from "@/hooks/use-calendar-navigate";
import { useCalendarUrlSync } from "@/hooks/use-calendar-url-sync";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  calendarViewAtom,
  eventPopoverAnchorAtom,
  selectedEventAtom,
} from "@/lib/atoms";

export function CalendarPage() {
  useCalendarUrlSync();
  const [view] = useAtom(calendarViewAtom);
  const navigate = useCalendarNavigate();
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
    (date: Date) => navigate({ view: "day", date }),
    [navigate],
  );

  const handleSelectWeek = useCallback(
    (date: Date) => navigate({ view: "week", date }),
    [navigate],
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
            onSelectWeek={handleSelectWeek}
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
