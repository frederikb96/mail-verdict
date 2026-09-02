"use client";

import { useState } from "react";
import { useAtom } from "jotai";
import { ChevronLeft, ChevronRight, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EventEditor } from "@/components/calendar/event-editor";
import { calendarDateAtom, calendarViewAtom, type CalendarViewMode } from "@/lib/atoms";
import { addDays, addMonths, addWeeks, format, startOfWeek } from "@/lib/dates";
import { useIsMobile } from "@/hooks/use-mobile";

const VIEWS: { value: CalendarViewMode; label: string }[] = [
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
  { value: "agenda", label: "Agenda" },
];

function titleFor(view: CalendarViewMode, date: Date): string {
  if (view === "day") return format(date, "EEEE, MMMM d, yyyy");
  if (view === "week") {
    const start = startOfWeek(date, { weekStartsOn: 1 });
    const end = addDays(start, 6);
    return format(start, "MMM d") + " – " + format(end, "MMM d, yyyy");
  }
  return format(date, "MMMM yyyy");
}

export function CalendarToolbar() {
  const [view, setView] = useAtom(calendarViewAtom);
  const [date, setDate] = useAtom(calendarDateAtom);
  const [createOpen, setCreateOpen] = useState(false);
  const isMobile = useIsMobile();

  const step = (dir: 1 | -1) => {
    if (view === "day") setDate((d) => addDays(d, dir));
    else if (view === "week") setDate((d) => addWeeks(d, dir));
    else setDate((d) => addMonths(d, dir));
  };

  const visibleViews = isMobile ? VIEWS.filter((v) => v.value !== "week") : VIEWS;

  return (
    <div className="flex flex-wrap items-center gap-2 border-b px-3 py-1.5">
      <Button variant="outline" size="sm" onClick={() => setDate(new Date())}>
        Today
      </Button>
      <div className="flex items-center gap-0.5">
        <Button variant="ghost" size="icon-sm" aria-label="Previous" onClick={() => step(-1)}>
          <ChevronLeft className="h-4 w-4" />
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label="Next" onClick={() => step(1)}>
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>
      <span className="text-sm font-medium">{titleFor(view, date)}</span>

      <div className="ml-auto flex items-center gap-2">
        <Tabs value={view} onValueChange={(v) => v && setView(v as CalendarViewMode)}>
          <TabsList>
            {visibleViews.map((v) => (
              <TabsTrigger key={v.value} value={v.value}>
                {v.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <Button size="sm" className="gap-1" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          New event
        </Button>
      </div>

      <EventEditor
        open={createOpen}
        onOpenChange={setCreateOpen}
        mode="create"
        defaultDate={date}
      />
    </div>
  );
}
