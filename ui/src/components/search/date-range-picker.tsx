"use client";

/**
 * Search's date-range control: drag either handle over a timeline
 * spanning the scope's own oldest and newest dated message, applied as
 * it moves (no apply button), dismissed the same way FolderPicker's own
 * popover already is -- reused rather than reimplemented, since a
 * hand-rolled outside-press handler cannot know about a portal it
 * doesn't name (see this repo's own notes on that trap).
 *
 * The slider's own domain is a fixed integer resolution (0..SLIDER_MAX),
 * not the millisecond timestamps themselves -- a range slider's two
 * thumbs are compared and pushed apart in that domain, and a domain this
 * coarse is already far finer than a drag gesture can place by hand.
 */

import { useMemo, useState } from "react";
import { format } from "date-fns";
import { CalendarRange, ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Slider } from "@/components/ui/slider";
import { useSearchDateBounds } from "@/hooks/use-search";
import type { SearchDateRange } from "@/lib/search-prefs";

const SLIDER_MAX = 1000;

interface DateRangePickerProps {
  value: SearchDateRange | null;
  onChange: (range: SearchDateRange | null) => void;
  accountId?: string;
  folderIds: string[] | null;
}

/** A handful of evenly spaced date labels along the axis -- month/year if
 * the scope spans two years or less (the case where a month means
 * something to point at), otherwise year alone. Not a density histogram:
 * that needs its own aggregate endpoint and is a larger, separate piece
 * of work than a labelled axis. */
function buildTicks(oldestMs: number, newestMs: number): { pos: number; label: string }[] {
  const spanMs = newestMs - oldestMs;
  const spanYears = spanMs / (365 * 24 * 3600 * 1000);
  const formatStr = spanYears <= 2 ? "MMM yyyy" : "yyyy";
  const tickCount = 5;
  return Array.from({ length: tickCount }, (_, i) => {
    const frac = i / (tickCount - 1);
    const ms = oldestMs + frac * spanMs;
    return { pos: frac * SLIDER_MAX, label: format(new Date(ms), formatStr) };
  });
}

export function DateRangePicker({ value, onChange, accountId, folderIds }: DateRangePickerProps) {
  const { data: bounds, isLoading } = useSearchDateBounds(accountId, folderIds);
  const [open, setOpen] = useState(false);

  const oldestMs = bounds?.oldest ? new Date(bounds.oldest).getTime() : null;
  const newestMs = bounds?.newest ? new Date(bounds.newest).getTime() : null;
  const hasScope = oldestMs !== null && newestMs !== null && newestMs > oldestMs;

  const ticks = useMemo(
    () => (hasScope ? buildTicks(oldestMs!, newestMs!) : []),
    [hasScope, oldestMs, newestMs],
  );

  const dateToPos = (iso: string): number => {
    if (!hasScope) return 0;
    const ms = new Date(iso).getTime();
    return Math.min(SLIDER_MAX, Math.max(0, ((ms - oldestMs!) / (newestMs! - oldestMs!)) * SLIDER_MAX));
  };
  const posToDate = (pos: number): string => {
    const ms = oldestMs! + (pos / SLIDER_MAX) * (newestMs! - oldestMs!);
    return new Date(ms).toISOString();
  };

  const positions: [number, number] = hasScope
    ? [value?.after ? dateToPos(value.after) : 0, value?.before ? dateToPos(value.before) : SLIDER_MAX]
    : [0, SLIDER_MAX];

  const handleValueChange = (next: number[]) => {
    if (!hasScope) return;
    const [lo, hi] = next;
    const atFullRange = lo <= 0 && hi >= SLIDER_MAX;
    onChange(
      atFullRange
        ? null
        : { after: lo > 0 ? posToDate(lo) : null, before: hi < SLIDER_MAX ? posToDate(hi) : null },
    );
  };

  const label = !value
    ? "All time"
    : `${value.after ? format(new Date(value.after), "MMM yyyy") : "…"} – ${
        value.before ? format(new Date(value.before), "MMM yyyy") : "…"
      }`;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger render={<Button variant="outline" size="sm" className="gap-1.5" />}>
        <CalendarRange className="h-3.5 w-3.5" />
        {label}
        <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-4">
        {isLoading && <div className="py-4 text-center text-xs text-muted-foreground">Loading…</div>}
        {!isLoading && !hasScope && (
          <div className="py-4 text-center text-xs text-muted-foreground">
            No dated messages in this scope
          </div>
        )}
        {!isLoading && hasScope && (
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between text-xs">
              <span className="font-medium text-muted-foreground">Date range</span>
              <button
                type="button"
                className="text-primary hover:underline disabled:pointer-events-none disabled:opacity-50"
                onClick={() => onChange(null)}
                disabled={!value}
              >
                Reset
              </button>
            </div>
            <div className="flex items-center justify-between text-xs font-medium">
              <span>{format(new Date(posToDate(positions[0])), "dd MMM yyyy")}</span>
              <span>{format(new Date(posToDate(positions[1])), "dd MMM yyyy")}</span>
            </div>
            <Slider
              min={0}
              max={SLIDER_MAX}
              step={1}
              thumbCount={2}
              value={positions}
              onValueChange={handleValueChange}
            />
            <div className="relative h-4 text-[10px] text-muted-foreground">
              {ticks.map((tick, i) => (
                <span
                  key={i}
                  className="absolute -translate-x-1/2 whitespace-nowrap"
                  style={{ left: `${(tick.pos / SLIDER_MAX) * 100}%` }}
                >
                  {tick.label}
                </span>
              ))}
            </div>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
