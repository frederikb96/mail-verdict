"use client";

/**
 * The small navigation month in the calendar sidebar. Unlike the main month
 * scroller this renders one month at a time and needs no virtualization --
 * 5-6 rows is not a scale problem.
 */

import { useEffect, useState } from "react";
import { useAtomValue } from "jotai";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useCalendarNavigate } from "@/hooks/use-calendar-navigate";
import { calendarDateAtom } from "@/lib/atoms";
import {
  addMonths,
  daysOfMonthGrid,
  format,
  isSameDay,
  isSameMonth,
  isToday,
  startOfMonth,
} from "@/lib/dates";
import { cn } from "@/lib/utils";

/** Reads the anchor date itself rather than taking it from the sidebar:
 * the month scroller writes that atom on every week row crossed, and
 * this is the only part of the sidebar that has to follow it. */
export function MiniMonth() {
  const anchor = useAtomValue(calendarDateAtom);
  const navigate = useCalendarNavigate();
  const [displayMonth, setDisplayMonth] = useState(() => startOfMonth(anchor));

  // The chevrons browse displayMonth on its own, ahead of the main view --
  // but once the anchor itself moves (Today, the toolbar arrows, the main
  // scroller), the mini-month follows it rather than being left behind.
  useEffect(() => {
    setDisplayMonth(startOfMonth(anchor));
  }, [anchor]);

  const days = daysOfMonthGrid(displayMonth);

  return (
    <div className="px-2 py-1">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-medium" data-testid="mini-month-title">
          {format(displayMonth, "MMMM yyyy")}
        </span>
        <div className="flex items-center gap-0.5">
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Previous month"
            onClick={() => setDisplayMonth((m) => addMonths(m, -1))}
          >
            <ChevronLeft className="h-3 w-3" />
          </Button>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Next month"
            onClick={() => setDisplayMonth((m) => addMonths(m, 1))}
          >
            <ChevronRight className="h-3 w-3" />
          </Button>
        </div>
      </div>
      <div className="grid grid-cols-7 gap-0.5 text-center text-[10px] text-muted-foreground">
        {["M", "T", "W", "T", "F", "S", "S"].map((d, i) => (
          <span key={i}>{d}</span>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-0.5">
        {days.map((day) => (
          <button
            key={day.toISOString()}
            type="button"
            onClick={() => navigate({ date: day })}
            className={cn(
              "flex h-6 w-6 items-center justify-center rounded-full text-xs",
              !isSameMonth(day, displayMonth) && "text-muted-foreground/40",
              isSameDay(day, anchor) && "bg-primary text-primary-foreground",
              !isSameDay(day, anchor) && isToday(day) && "font-semibold text-primary",
              !isSameDay(day, anchor) && "hover:bg-muted",
            )}
          >
            {day.getDate()}
          </button>
        ))}
      </div>
    </div>
  );
}
