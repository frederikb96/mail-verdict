"use client";

/** The "+N more" popover: every event on one day, anchored to its cell. */

import { useMemo } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EventChip } from "@/components/calendar/event-chip";
import type { SelectEventHandler } from "@/components/calendar/layout";
import { useCalendars } from "@/hooks/use-calendars";
import { format } from "@/lib/dates";
import type { EventInstance } from "@/types/api";

interface DayEventsPopoverProps {
  date: Date;
  events: EventInstance[];
  onSelectEvent: SelectEventHandler;
  onClose: () => void;
}

export function DayEventsPopover({ date, events, onSelectEvent, onClose }: DayEventsPopoverProps) {
  const { data: calendars } = useCalendars();
  const calendarById = useMemo(
    () => new Map((calendars ?? []).map((c) => [c.id, c])),
    [calendars],
  );
  const sorted = [...events].sort((a, b) => a.dtstart.localeCompare(b.dtstart));

  return (
    <div
      className="absolute z-40 flex w-64 flex-col gap-1 rounded-lg border bg-popover p-2 shadow-md"
      style={{ top: 24, left: "10%" }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center justify-between px-1">
        <span className="text-sm font-medium">{format(date, "EEEE, MMM d")}</span>
        <Button variant="ghost" size="icon-xs" onClick={onClose}>
          <X className="h-3 w-3" />
        </Button>
      </div>
      <div className="flex max-h-64 flex-col gap-1 overflow-y-auto">
        {sorted.map((e) => (
          <EventChip
            key={`${e.object_id}:${e.recurrence_id ?? "master"}`}
            event={e}
            calendar={calendarById.get(e.calendar_id)}
            variant="agenda"
            timeLabel={e.all_day ? "All day" : format(new Date(e.dtstart), "HH:mm")}
            onClick={(ev) => {
              ev.stopPropagation();
              onSelectEvent(e.object_id, e.recurrence_id, ev);
              onClose();
            }}
          />
        ))}
        {sorted.length === 0 && (
          <span className="px-1 py-2 text-sm text-muted-foreground">Nothing scheduled</span>
        )}
      </div>
    </div>
  );
}
