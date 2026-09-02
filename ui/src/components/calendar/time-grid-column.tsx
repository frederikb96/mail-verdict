"use client";

/** One day column of the time grid: positioned blocks and the pointer
 * surface that drives move/resize/create. */

import { useMemo } from "react";
import { useAtomValue } from "jotai";
import { EventChip } from "@/components/calendar/event-chip";
import { packColumns, type SelectEventHandler, type TimedItem } from "@/components/calendar/layout";
import { useCalendars } from "@/hooks/use-calendars";
import type { GridGhost } from "@/hooks/use-grid-drag";
import { selectedEventAtom } from "@/lib/atoms";
import { format, isToday } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { EventInstance } from "@/types/api";

interface TimedInstance extends TimedItem {
  event: EventInstance;
}

interface TimeGridColumnProps {
  date: Date;
  column: number;
  events: EventInstance[];
  hourHeight: number;
  ghost: GridGhost | null;
  onSelectEvent: SelectEventHandler;
  onPointerDownMove: (
    e: React.PointerEvent,
    event: EventInstance,
    startMin: number,
    endMin: number,
    kind: "move" | "resize-start" | "resize-end",
  ) => void;
  onPointerDownCreate: (e: React.PointerEvent) => void;
}

function minutesOf(date: Date): number {
  return date.getHours() * 60 + date.getMinutes();
}

export function TimeGridColumn({
  date,
  column,
  events,
  hourHeight,
  ghost,
  onSelectEvent,
  onPointerDownMove,
  onPointerDownCreate,
}: TimeGridColumnProps) {
  const { data: calendars } = useCalendars();
  const selected = useAtomValue(selectedEventAtom);
  const calendarById = useMemo(() => new Map((calendars ?? []).map((c) => [c.id, c])), [calendars]);

  const timed: TimedInstance[] = events.map((e) => {
    const start = new Date(e.dtstart);
    const end = new Date(e.dtend);
    const startMin = isSameCalendarDay(start, date) ? minutesOf(start) : 0;
    const endMin = isSameCalendarDay(end, date) ? minutesOf(end) : 24 * 60;
    return { key: `${e.object_id}:${e.recurrence_id ?? "master"}`, startMin, endMin, event: e };
  });

  const packed = packColumns(timed);
  const pxPerMin = hourHeight / 60;

  return (
    <div
      className="relative flex-1 border-r last:border-r-0"
      data-grid-surface
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onPointerDownCreate(e);
      }}
    >
      {Array.from({ length: 24 }).map((_, h) => (
        <div key={h} className="border-b" style={{ height: hourHeight }} />
      ))}

      {isToday(date) && <NowLine hourHeight={hourHeight} />}

      {packed.map(({ item, column: col, columns, expandable }) => {
        const width = expandable ? 100 - (col / columns) * 100 : 100 / columns;
        const left = (col / columns) * 100;
        return (
          <EventChip
            key={item.key}
            event={item.event}
            calendar={calendarById.get(item.event.calendar_id)}
            variant="grid"
            selected={selected?.objectId === item.event.object_id}
            timeLabel={format(new Date(item.event.dtstart), "HH:mm")}
            style={{
              top: item.startMin * pxPerMin,
              height: Math.max(18, (item.endMin - item.startMin) * pxPerMin),
              left: `${left}%`,
              width: `${width}%`,
            }}
            onClick={(ev) => {
              ev.stopPropagation();
              onSelectEvent(item.event.object_id, item.event.recurrence_id, ev);
            }}
            onPointerDown={(ev) => {
              const target = ev.target as HTMLElement;
              const rect = ev.currentTarget.getBoundingClientRect();
              const offsetY = ev.clientY - rect.top;
              const kind =
                offsetY < 6 ? "resize-start" : offsetY > rect.height - 6 ? "resize-end" : "move";
              void target;
              onPointerDownMove(ev, item.event, item.startMin, item.endMin, kind);
            }}
          />
        );
      })}

      {ghost && ghost.column === column && ghost.objectId !== "__new__" && (
        <div
          className="pointer-events-none absolute inset-x-0 rounded-md border-2 border-dashed border-primary bg-primary/10"
          style={{
            top: ghost.startMin * pxPerMin,
            height: Math.max(18, (ghost.endMin - ghost.startMin) * pxPerMin),
          }}
        />
      )}
      {ghost && ghost.column === column && ghost.objectId === "__new__" && (
        <div
          className="pointer-events-none absolute inset-x-1 rounded-md bg-primary/20 text-xs"
          style={{
            top: ghost.startMin * pxPerMin,
            height: Math.max(18, (ghost.endMin - ghost.startMin) * pxPerMin),
          }}
        >
          <span className={cn("px-1 text-primary")}>
            {formatMinutes(ghost.startMin)}–{formatMinutes(ghost.endMin)}
          </span>
        </div>
      )}
    </div>
  );
}

function isSameCalendarDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function formatMinutes(min: number): string {
  const h = Math.floor(min / 60) % 24;
  const m = min % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function NowLine({ hourHeight }: { hourHeight: number }) {
  const now = new Date();
  const top = (now.getHours() * 60 + now.getMinutes()) * (hourHeight / 60);
  return (
    <div className="pointer-events-none absolute inset-x-0 z-10 flex items-center" style={{ top }}>
      <span className="-ml-1 h-2 w-2 rounded-full bg-destructive" />
      <span className="h-px flex-1 bg-destructive" />
    </div>
  );
}
