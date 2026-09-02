"use client";

/**
 * A VList of day-header rows and event rows -- the phone's landing view,
 * and optionally the desktop's list alternative. Uniform row heights (32px
 * header, 56px event), so this is exactly the "library handles it" case the
 * scrolling skill describes, unlike the month view.
 */

import { useMemo, useRef } from "react";
import { useAtomValue } from "jotai";
import { VList, type VListHandle } from "virtua";
import { CalendarX2 } from "lucide-react";
import { EventChip } from "@/components/calendar/event-chip";
import type { SelectEventHandler } from "@/components/calendar/layout";
import { useCalendars } from "@/hooks/use-calendars";
import { useEventsForRange } from "@/hooks/use-events";
import { calendarDateAtom } from "@/lib/atoms";
import { addDays, format, isToday } from "@/lib/dates";

const AGENDA_RANGE_DAYS = 60;
const HEADER_HEIGHT = 32;
const EVENT_HEIGHT = 56;

type AgendaRow =
  | { kind: "header"; date: Date }
  | { kind: "event"; date: Date; event: import("@/types/api").EventInstance };

interface AgendaListProps {
  onSelectEvent: SelectEventHandler;
}

export function AgendaList({ onSelectEvent }: AgendaListProps) {
  const anchor = useAtomValue(calendarDateAtom);
  const { data: calendars } = useCalendars();
  const calendarById = useMemo(() => new Map((calendars ?? []).map((c) => [c.id, c])), [calendars]);
  const vlistRef = useRef<VListHandle>(null);

  const rangeStart = useMemo(() => {
    const d = new Date(anchor);
    d.setHours(0, 0, 0, 0);
    return d;
  }, [anchor]);
  const rangeEnd = useMemo(() => addDays(rangeStart, AGENDA_RANGE_DAYS), [rangeStart]);

  const { events, isLoading } = useEventsForRange(rangeStart, rangeEnd);

  const rows = useMemo<AgendaRow[]>(() => {
    const byDay = new Map<string, typeof events>();
    for (const e of events) {
      const day = new Date(e.dtstart);
      day.setHours(0, 0, 0, 0);
      const key = day.toISOString();
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key)!.push(e);
    }
    const result: AgendaRow[] = [];
    for (let d = new Date(rangeStart); d <= rangeEnd; d = addDays(d, 1)) {
      const key = d.toISOString();
      const dayEvents = (byDay.get(key) ?? []).sort((a, b) => a.dtstart.localeCompare(b.dtstart));
      if (dayEvents.length === 0) continue;
      result.push({ kind: "header", date: new Date(d) });
      for (const event of dayEvents) result.push({ kind: "event", date: new Date(d), event });
    }
    return result;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, rangeStart.getTime(), rangeEnd.getTime()]);

  if (isLoading && rows.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <CalendarX2 className="h-12 w-12 opacity-50" />
        <p className="text-sm">Nothing scheduled in the next {AGENDA_RANGE_DAYS} days</p>
      </div>
    );
  }

  return (
    <VList ref={vlistRef} className="flex-1" style={{ height: "100%" }}>
      {rows.map((row, i) =>
        row.kind === "header" ? (
          <div
            key={`h-${i}`}
            style={{ height: HEADER_HEIGHT }}
            className="flex items-center gap-2 border-b bg-muted/30 px-3"
          >
            <span className="text-xs font-medium">{format(row.date, "EEEE, MMM d")}</span>
            {isToday(row.date) && (
              <span className="rounded-full bg-primary px-1.5 py-0.5 text-[10px] text-primary-foreground">
                Today
              </span>
            )}
          </div>
        ) : (
          <div key={`${row.event.object_id}:${row.event.recurrence_id ?? "master"}-${i}`} style={{ height: EVENT_HEIGHT }} className="px-3 py-1.5">
            <EventChip
              event={row.event}
              calendar={calendarById.get(row.event.calendar_id)}
              variant="agenda"
              timeLabel={row.event.all_day ? "All day" : format(new Date(row.event.dtstart), "HH:mm")}
              onClick={(ev) => onSelectEvent(row.event.object_id, row.event.recurrence_id, ev)}
              className="h-full"
            />
          </div>
        ),
      )}
    </VList>
  );
}
