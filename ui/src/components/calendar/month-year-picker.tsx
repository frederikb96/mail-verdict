"use client";

/** Clicking the toolbar's month/year title opens this instead of stepping
 * one unit at a time -- a month grid for the current year first, and
 * clicking the year switches to a year grid to jump further. */

import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

const MONTH_LABELS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];
const YEARS_PER_PAGE = 12;

interface MonthYearPickerProps {
  anchor: Date;
  onSelect: (date: Date) => void;
  children: React.ReactNode;
}

export function MonthYearPicker({ anchor, onSelect, children }: MonthYearPickerProps) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<"month" | "year">("month");
  const [year, setYear] = useState(anchor.getFullYear());
  const [decadeStart, setDecadeStart] = useState(
    () => Math.floor(anchor.getFullYear() / YEARS_PER_PAGE) * YEARS_PER_PAGE,
  );

  // Re-anchor to wherever the toolbar currently is each time the popover
  // opens -- otherwise a picker left on a year picked three opens ago
  // reappears there instead of following the calendar's own navigation.
  useEffect(() => {
    if (!open) return;
    setPage("month");
    setYear(anchor.getFullYear());
    setDecadeStart(Math.floor(anchor.getFullYear() / YEARS_PER_PAGE) * YEARS_PER_PAGE);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger render={<button type="button" />}>{children}</PopoverTrigger>
      <PopoverContent className="w-64 p-2" align="start">
        {page === "month" ? (
          <>
            <div className="mb-2 flex items-center justify-between">
              <Button variant="ghost" size="icon-xs" aria-label="Previous year" onClick={() => setYear((y) => y - 1)}>
                <ChevronLeft className="h-3.5 w-3.5" />
              </Button>
              <button
                type="button"
                className="rounded px-2 py-0.5 text-sm font-medium hover:bg-muted"
                onClick={() => setPage("year")}
              >
                {year}
              </button>
              <Button variant="ghost" size="icon-xs" aria-label="Next year" onClick={() => setYear((y) => y + 1)}>
                <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            </div>
            <div className="grid grid-cols-3 gap-1">
              {MONTH_LABELS.map((label, i) => (
                <button
                  key={label}
                  type="button"
                  className={cn(
                    "rounded-md px-2 py-1.5 text-sm hover:bg-muted",
                    year === anchor.getFullYear() && i === anchor.getMonth() && "bg-primary text-primary-foreground",
                  )}
                  onClick={() => {
                    onSelect(new Date(year, i, 1));
                    setOpen(false);
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
          </>
        ) : (
          <>
            <div className="mb-2 flex items-center justify-between">
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Previous decade"
                onClick={() => setDecadeStart((d) => d - YEARS_PER_PAGE)}
              >
                <ChevronLeft className="h-3.5 w-3.5" />
              </Button>
              <span className="text-sm font-medium">
                {decadeStart} – {decadeStart + YEARS_PER_PAGE - 1}
              </span>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Next decade"
                onClick={() => setDecadeStart((d) => d + YEARS_PER_PAGE)}
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            </div>
            <div className="grid grid-cols-3 gap-1">
              {Array.from({ length: YEARS_PER_PAGE }, (_, i) => decadeStart + i).map((y) => (
                <button
                  key={y}
                  type="button"
                  className={cn(
                    "rounded-md px-2 py-1.5 text-sm hover:bg-muted",
                    y === anchor.getFullYear() && "bg-primary text-primary-foreground",
                  )}
                  onClick={() => {
                    setYear(y);
                    setPage("month");
                  }}
                >
                  {y}
                </button>
              ))}
            </div>
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}
