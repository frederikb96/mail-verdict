"use client";

/**
 * Search's time window control: a clock button that opens a menu of
 * presets, a slider overlay over the last 52 weeks and a custom range of
 * days. The window itself, and everything that resolves it against the
 * clock, lives in lib/search-window.ts.
 */

import { useLayoutEffect, useRef, useState } from "react";
import { Clock, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { cn } from "@/lib/utils";
import {
  SLIDER_WEEKS,
  WINDOW_PRESETS,
  formatDay,
  labelledTicks,
  monthTicks,
  parseDay,
  resolveWindow,
  sliderStopDate,
  sliderStops,
  sliderWindow,
  windowLabel,
  type SearchWindow,
} from "@/lib/search-window";

interface TimePickerProps {
  value: SearchWindow | null;
  onChange: (window: SearchWindow | null) => void;
}

export function TimePicker({ value, onChange }: TimePickerProps) {
  const [sliderOpen, setSliderOpen] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const label = value ? `Time window: ${windowLabel(value)}` : "Time window";

  const openSlider = () => {
    // Opening with no slider window yet selects the whole axis at once, so
    // closing without touching anything means "the last 52 weeks".
    if (value?.kind !== "slider") onChange(sliderWindow(0, SLIDER_WEEKS, new Date()));
    setSliderOpen(true);
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              variant="outline"
              size="icon"
              title={label}
              aria-label={label}
              data-testid="search-time-picker"
              data-active={value ? "true" : undefined}
              className={cn(value && "border-primary bg-primary/10 text-primary hover:bg-primary/15")}
            />
          }
        >
          <Clock className="h-4 w-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {WINDOW_PRESETS.map(({ preset, label: presetLabel }) => (
            <DropdownMenuItem key={preset} onClick={() => onChange({ kind: "preset", preset })}>
              {presetLabel}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={openSlider}>Slider…</DropdownMenuItem>
          <DropdownMenuItem onClick={() => setCustomOpen(true)}>Custom…</DropdownMenuItem>
          {value && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={() => onChange(null)}>Clear</DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      <SliderOverlay
        open={sliderOpen}
        onOpenChange={setSliderOpen}
        value={value}
        onChange={onChange}
      />
      <CustomRangeDialog
        open={customOpen}
        onOpenChange={setCustomOpen}
        value={value}
        onChange={onChange}
      />
    </>
  );
}

/** The active window as a clearable chip, for the row of active filters. */
export function TimeWindowChip({
  value,
  onClear,
}: {
  value: SearchWindow;
  onClear: () => void;
}) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-primary bg-primary/10 py-0.5 pl-2 pr-1 text-xs"
      data-testid="search-time-chip"
    >
      {windowLabel(value)}
      <button
        type="button"
        onClick={onClear}
        aria-label="Clear time window"
        className="rounded-full p-0.5 text-muted-foreground hover:text-foreground"
      >
        <X className="h-3 w-3" />
      </button>
    </span>
  );
}

interface WindowDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  value: SearchWindow | null;
  onChange: (window: SearchWindow | null) => void;
}

/** Mounts its body only while open, so each opening starts from the
 * window in force at that moment. */
function SliderOverlay({ open, onOpenChange, value, onChange }: WindowDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <DialogContent
          size="xl"
          className="gap-6 p-6"
          data-testid="search-time-slider"
          onKeyDown={(e) => {
            if (e.key === "Enter") onOpenChange(false);
          }}
        >
          <SliderBody value={value} onChange={onChange} />
        </DialogContent>
      )}
    </Dialog>
  );
}

function SliderBody({
  value,
  onChange,
}: {
  value: SearchWindow | null;
  onChange: (window: SearchWindow | null) => void;
}) {
  const now = new Date();
  // Live position while dragging; the search only changes on release.
  const [stops, setStops] = useState<[number, number]>(() => sliderStops(value, now));
  const [a, b] = stops;

  return (
    <>
      <DialogHeader>
        <DialogTitle>Time window</DialogTitle>
      </DialogHeader>
      <div className="flex items-center justify-between text-base font-medium" data-testid="search-time-slider-range">
        <span>{formatDay(sliderStopDate(a, now))}</span>
        <span>{b >= SLIDER_WEEKS ? "now" : formatDay(sliderStopDate(b, now))}</span>
      </div>
      <div className="px-3">
        <Slider
          min={0}
          max={SLIDER_WEEKS}
          step={1}
          minStepsBetweenValues={1}
          thumbCount={2}
          value={stops}
          thumbClassName="h-8 w-8"
          trackClassName="h-2.5"
          onValueChange={(next) => setStops([next[0], next[1]])}
          onValueCommitted={(next) => onChange(sliderWindow(next[0], next[1], new Date()))}
        />
        <MonthAxis now={now} />
      </div>
    </>
  );
}

/** One mark per month under the slider; labels only where they fit the
 * width the axis has (a phone shows fewer than a desktop). */
function MonthAxis({ now }: { now: Date }) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const observer = new ResizeObserver(() => setWidth(el.clientWidth));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const ticks = monthTicks(now);
  const labelled = labelledTicks(ticks, width);
  return (
    <div ref={ref} className="relative mt-1 h-8 text-xs text-muted-foreground" aria-hidden>
      {ticks.map((tick) => (
        <div
          key={tick.stop}
          className="absolute top-0 flex -translate-x-1/2 flex-col items-center gap-0.5"
          style={{ left: `${(tick.stop / SLIDER_WEEKS) * 100}%` }}
        >
          <span className={cn("w-px bg-border", tick.isYear ? "h-2.5 bg-foreground/50" : "h-1.5")} />
          {labelled.has(tick.stop) && (
            <span className={cn("whitespace-nowrap", tick.isYear && "font-medium text-foreground")}>
              {tick.label}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

function CustomRangeDialog({ open, onOpenChange, value, onChange }: WindowDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <DialogContent size="sm" data-testid="search-time-custom">
          <CustomForm
            value={value}
            onApply={(window) => {
              onChange(window);
              onOpenChange(false);
            }}
            onCancel={() => onOpenChange(false)}
          />
        </DialogContent>
      )}
    </Dialog>
  );
}

function CustomForm({
  value,
  onApply,
  onCancel,
}: {
  value: SearchWindow | null;
  onApply: (window: SearchWindow) => void;
  onCancel: () => void;
}) {
  const [since, setSince] = useState(() => {
    const now = new Date();
    return formatDay(value ? new Date(resolveWindow(value, now).after) : now);
  });
  const [until, setUntil] = useState(() => {
    const now = new Date();
    return formatDay(value ? new Date(resolveWindow(value, now).before) : now);
  });

  const sinceDay = parseDay(since);
  const untilDay = parseDay(until);
  const valid = sinceDay !== null && untilDay !== null && sinceDay <= untilDay;

  return (
    <form
      className="grid gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) onApply({ kind: "custom", since: since.trim(), until: until.trim() });
      }}
    >
      <DialogHeader>
        <DialogTitle>Custom range</DialogTitle>
      </DialogHeader>
      <label className="grid gap-1 text-xs text-muted-foreground">
        From
        <Input
          value={since}
          onChange={(e) => setSince(e.target.value)}
          placeholder="YYYY-MM-DD"
          inputMode="numeric"
          aria-invalid={sinceDay === null}
          data-testid="search-time-custom-since"
        />
      </label>
      <label className="grid gap-1 text-xs text-muted-foreground">
        To
        <Input
          value={until}
          onChange={(e) => setUntil(e.target.value)}
          placeholder="YYYY-MM-DD"
          inputMode="numeric"
          aria-invalid={untilDay === null}
          data-testid="search-time-custom-until"
        />
      </label>
      <p className="text-xs text-muted-foreground">Both days are included.</p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" disabled={!valid} data-testid="search-time-custom-apply">
          Apply
        </Button>
      </DialogFooter>
    </form>
  );
}
