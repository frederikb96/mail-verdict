"use client";

import { useState } from "react";
import { useAtom } from "jotai";
import { ChevronLeft, ChevronRight, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EventEditor } from "@/components/calendar/event-editor";
import { MonthYearPicker } from "@/components/calendar/month-year-picker";
import { calendarDateAtom, calendarViewAtom, type CalendarViewMode } from "@/lib/atoms";
import { addDays, addMonths, addWeeks, format, startOfWeek, weekNumber } from "@/lib/dates";
import { useCalendarNavigate } from "@/hooks/use-calendar-navigate";
import { useCalendarShortcuts } from "@/hooks/use-calendar-shortcuts";
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
    const range = format(start, "MMM d") + " – " + format(end, "MMM d, yyyy");
    return `${range} (Week ${weekNumber(date)})`;
  }
  return format(date, "MMMM yyyy");
}

export function CalendarToolbar() {
  const [view] = useAtom(calendarViewAtom);
  const [date] = useAtom(calendarDateAtom);
  const navigate = useCalendarNavigate();
  const [createOpen, setCreateOpen] = useState(false);
  const isMobile = useIsMobile();

  // push: false -- prev/next never spends a history entry (navigate()'s
  // default push is for the changes worth one), but still keeps the URL
  // in step via replace.
  const step = (dir: 1 | -1) => {
    if (view === "day") navigate({ date: addDays(date, dir) }, { push: false });
    else if (view === "week") navigate({ date: addWeeks(date, dir) }, { push: false });
    else navigate({ date: addMonths(date, dir) }, { push: false });
  };

  const visibleViews = isMobile ? VIEWS.filter((v) => v.value !== "week") : VIEWS;

  useCalendarShortcuts({ onCreate: () => setCreateOpen(true) });

  return (
    <div className="flex flex-wrap items-center gap-2 border-b px-3 py-1.5">
      <Button variant="outline" size="sm" onClick={() => navigate({ date: new Date() })}>
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
      <MonthYearPicker anchor={date} onSelect={(d) => navigate({ date: d })}>
        <span
          className="rounded px-1 text-sm font-medium hover:bg-muted"
          data-testid="calendar-toolbar-title"
        >
          {titleFor(view, date)}
        </span>
      </MonthYearPicker>

      <div className="ml-auto flex items-center gap-2">
        <Tabs value={view} onValueChange={(v) => v && navigate({ view: v as CalendarViewMode })}>
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
