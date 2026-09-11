"use client";

/**
 * A VList of day-header rows and event rows -- the phone's landing view,
 * and optionally the desktop's list alternative. Uniform row heights (32px
 * header, 32px event), so this is exactly the "library handles it" case the
 * scrolling skill describes, unlike the month view.
 */

import { useMemo, useRef } from "react";
import { useAtomValue } from "jotai";
import { VList, type VListHandle } from "virtua";
import { AlertTriangle, Ban, CalendarX2, Loader2, Repeat } from "lucide-react";
import { resolveCalendarColor } from "@/components/calendar/colors";
import { allDayInstant, deriveEventLook, type SelectEventHandler } from "@/components/calendar/layout";
import { Truncate } from "@/components/ui/truncate";
import { useCalendars } from "@/hooks/use-calendars";
import { useEventsForRange } from "@/hooks/use-events";
import { calendarDateAtom } from "@/lib/atoms";
import { addDays, format, isToday } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { Calendar, EventInstance } from "@/types/api";

const AGENDA_RANGE_DAYS = 60;
const HEADER_HEIGHT = 32;
const EVENT_HEIGHT = 32;

/** One dense list row: time | colour dot | title | muted location and
 * calendar -- deliberately not the shared EventChip's pill look, which
 * reads as a full-width tinted block at this row height. State (pending,
 * failed, cancelled, recurring) still comes from deriveEventLook, so the
 * agenda cannot disagree with any other view about what an event means. */
function AgendaEventRow({
  event,
  calendar,
  timeLabel,
  onClick,
}: {
  event: EventInstance;
  calendar: Calendar | undefined;
  timeLabel: string;
  onClick: (e: React.MouseEvent) => void;
}) {
  const look = deriveEventLook(event);
  const color = calendar ? resolveCalendarColor(calendar) : "var(--muted-foreground)";
  const secondary = [event.location, calendar?.display_name].filter(Boolean).join(" · ");

  return (
    <div
      data-testid="event"
      data-event-id={event.object_id}
      data-recurrence-id={event.recurrence_id ?? ""}
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onClick(e as unknown as React.MouseEvent);
      }}
      className="flex h-full min-w-0 cursor-pointer items-center gap-2 rounded px-1 outline-none hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring"
    >
      <span className="w-11 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
        {timeLabel}
      </span>
      <span
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ backgroundColor: look.cancelled ? "var(--muted-foreground)" : color }}
      />
      {look.pending && <Loader2 className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />}
      {look.failed && !look.pending && (
        <AlertTriangle className="h-3 w-3 shrink-0 text-destructive" />
      )}
      {look.replyNotSent && !look.failed && (
        <AlertTriangle className="h-3 w-3 shrink-0 text-amber-500" />
      )}
      {look.cancelled && <Ban className="h-3 w-3 shrink-0 text-muted-foreground" />}
      {look.recurring && !look.cancelled && (
        <Repeat className="h-2.5 w-2.5 shrink-0 text-muted-foreground opacity-70" />
      )}
      <span
        className={cn(
          "min-w-0 flex-1 text-sm",
          look.cancelled && "text-muted-foreground line-through",
        )}
      >
        <Truncate text={event.summary || "(no title)"} />
      </span>
      {secondary && (
        <span className="hidden shrink-0 truncate text-xs text-muted-foreground sm:inline sm:max-w-[40%]">
          {secondary}
        </span>
      )}
    </div>
  );
}

type AgendaRow =
  | { kind: "header"; date: Date }
  | { kind: "event"; date: Date; event: EventInstance };

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
      const day = e.all_day ? allDayInstant(e.dtstart) : new Date(e.dtstart);
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
          <div key={`${row.event.object_id}:${row.event.recurrence_id ?? "master"}-${i}`} style={{ height: EVENT_HEIGHT }} className="px-3">
            <AgendaEventRow
              event={row.event}
              calendar={calendarById.get(row.event.calendar_id)}
              timeLabel={row.event.all_day ? "All day" : format(new Date(row.event.dtstart), "HH:mm")}
              onClick={(ev) => onSelectEvent(row.event.object_id, row.event.recurrence_id, ev)}
            />
          </div>
        ),
      )}
    </VList>
  );
}
