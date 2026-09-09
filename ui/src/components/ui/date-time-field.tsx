"use client";

/**
 * A day-first, 24-hour date/time field this application owns outright.
 * The native `datetime-local`/`date` control renders in the browser's own
 * locale (month-first, 12-hour, for an English-locale browser reading
 * German expectations) and its picker popup is browser UI no CSS here can
 * stop clipping against a right-anchored Sheet. This is a plain text
 * input -- typing is how it is actually used -- parsed on blur/Enter
 * rather than per keystroke, plus an app-rendered popover (a bottom sheet
 * on a phone) for picking a day and, for a timed field, an hour and
 * minute.
 */

import { useEffect, useId, useState } from "react";
import { CalendarIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  DATE_DISPLAY_FORMAT,
  DATE_TIME_DISPLAY_FORMAT,
  addMonths,
  daysOfMonthGrid,
  format,
  isSameDay,
  isSameMonth,
  isToday,
  startOfMonth,
  toWholeDayValue,
  wholeDayIso,
} from "@/lib/dates";
import { cn } from "@/lib/utils";

interface DateTimeFieldProps {
  id?: string;
  /** An instant, ISO 8601. */
  value: string;
  onChange: (iso: string) => void;
  /** "date" for an all-day field -- reuses the app's own literal-UTC-
   * midnight encoding (toWholeDayValue/wholeDayIso) rather than a second
   * one, the same rule the event editor's all-day toggle already follows. */
  mode?: "datetime" | "date";
  disabled?: boolean;
  /** The event's own bound zone, when it has one. Shown as a caption
   * whenever it differs from the browser's -- never silently converted;
   * a zone picker is out of scope here. */
  tz?: string | null;
  /** Told whenever the field's own typed text does or doesn't currently
   * parse, so the caller can gate Save without re-deriving the same
   * predicate the field already computed. */
  onValidityChange?: (valid: boolean) => void;
}

const WEEKDAY_NAMES = [
  "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
];

function startOfLocalDay(d: Date): Date {
  const r = new Date(d);
  r.setHours(0, 0, 0, 0);
  return r;
}

function addLocalDays(d: Date, days: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + days);
  return r;
}

function normalizeYear(y: number): number {
  return y < 100 ? 2000 + y : y;
}

/** date-only input keeps the time of day `current` already carries --
 * both grid picks and every relative keyword below name a day, never a
 * time, so this is the one merge every one of them shares. */
function combineDatePart(datePart: Date, current: Date, allDay: boolean): Date {
  if (allDay) return datePart;
  const result = new Date(datePart);
  result.setHours(current.getHours(), current.getMinutes(), 0, 0);
  return result;
}

/**
 * Parses free-typed text against `current` (the field's own existing
 * value, providing whichever half -- date or time -- the text doesn't
 * name) and `now` (real wall-clock time, what a relative keyword or
 * offset means). Returns null for text this recognises nothing in -- the
 * caller keeps the typed text on screen rather than clearing or
 * reverting it, both of which would hide the mistake.
 */
function parseDateTimeInput(
  raw: string, current: Date, now: Date, allDay: boolean,
): Date | null {
  const text = raw.trim();
  if (!text) return null;
  const lower = text.toLowerCase();

  if (lower === "today") return combineDatePart(startOfLocalDay(now), current, allDay);
  if (lower === "tomorrow") return combineDatePart(addLocalDays(startOfLocalDay(now), 1), current, allDay);

  const weekdayIndex = WEEKDAY_NAMES.findIndex(
    (name) => name === lower || name.slice(0, 3) === lower,
  );
  if (weekdayIndex >= 0) {
    const today = startOfLocalDay(now);
    let delta = (weekdayIndex - today.getDay() + 7) % 7;
    if (delta === 0) delta = 7; // "the next occurrence" -- not today, even if today matches
    return combineDatePart(addLocalDays(today, delta), current, allDay);
  }

  const offset = /^\+(\d+)([dw])$/.exec(lower);
  if (offset) {
    const amount = Number(offset[1]);
    const days = offset[2] === "w" ? amount * 7 : amount;
    return combineDatePart(addLocalDays(startOfLocalDay(now), days), current, allDay);
  }

  // Absolute date, with or without a time part: dd.MM.yyyy[ HH:mm], and
  // the same with / or - separators.
  const dateTime = /^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})(?:[ t](\d{1,2}):(\d{2}))?$/i.exec(text);
  if (dateTime) {
    const [, dRaw, mRaw, yRaw, hRaw, minRaw] = dateTime;
    const date = new Date(current);
    date.setFullYear(normalizeYear(Number(yRaw)), Number(mRaw) - 1, Number(dRaw));
    if (hRaw !== undefined) date.setHours(Number(hRaw), Number(minRaw), 0, 0);
    if (Number.isNaN(date.getTime())) return null;
    // setFullYear/setHours roll an out-of-range field into the next one
    // (day 32 becomes the 1st of the following month) rather than
    // rejecting it -- reading the result back out and comparing catches
    // that, which the NaN check above cannot.
    if (
      date.getDate() !== Number(dRaw) || date.getMonth() !== Number(mRaw) - 1 ||
      (hRaw !== undefined && (date.getHours() !== Number(hRaw) || date.getMinutes() !== Number(minRaw)))
    ) {
      return null;
    }
    return date;
  }

  if (!allDay) {
    // Time only: "9:30", "930", "9".
    const withColon = /^(\d{1,2}):(\d{2})$/.exec(text);
    const bareDigits = /^(\d{1,4})$/.exec(text);
    if (withColon || bareDigits) {
      let hour: number;
      let minute: number;
      if (withColon) {
        hour = Number(withColon[1]);
        minute = Number(withColon[2]);
      } else {
        const digits = bareDigits![1];
        if (digits.length <= 2) {
          hour = Number(digits);
          minute = 0;
        } else {
          minute = Number(digits.slice(-2));
          hour = Number(digits.slice(0, -2));
        }
      }
      if (hour > 23 || minute > 59) return null;
      const date = new Date(current);
      date.setHours(hour, minute, 0, 0);
      return date;
    }
  }

  return null;
}

/** toWholeDayValue reads the stored literal-UTC day; this reconstructs it
 * as a local midnight Date so date-fns' locale-aware `format`/grid helpers
 * (all of which read local getters) render and compare it correctly. */
function wholeDayLocalDate(iso: string): Date {
  const [year, month, day] = toWholeDayValue(iso).split("-").map(Number);
  return new Date(year, month - 1, day);
}

function displayValue(value: string, allDay: boolean): string {
  const d = allDay ? wholeDayLocalDate(value) : new Date(value);
  return format(d, allDay ? DATE_DISPLAY_FORMAT : DATE_TIME_DISPLAY_FORMAT);
}

interface MiniCalendarProps {
  selected: Date;
  onSelect: (day: Date) => void;
}

/** The grid half of the popover -- day-level only; month/year are browsed
 * with the chevrons, the same interaction the toolbar's own MonthYearPicker
 * offers for month/year alone. */
function MiniCalendar({ selected, onSelect }: MiniCalendarProps) {
  const [displayMonth, setDisplayMonth] = useState(() => startOfMonth(selected));
  useEffect(() => {
    setDisplayMonth(startOfMonth(selected));
  }, [selected]);

  const days = daysOfMonthGrid(displayMonth);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <Button
          type="button" variant="ghost" size="icon-xs" aria-label="Previous month"
          onClick={() => setDisplayMonth((m) => addMonths(m, -1))}
        >
          ‹
        </Button>
        <span className="text-sm font-medium">{format(displayMonth, "MMMM yyyy")}</span>
        <Button
          type="button" variant="ghost" size="icon-xs" aria-label="Next month"
          onClick={() => setDisplayMonth((m) => addMonths(m, 1))}
        >
          ›
        </Button>
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
            onClick={() => onSelect(day)}
            className={cn(
              "flex h-7 w-7 items-center justify-center rounded-full text-xs",
              !isSameMonth(day, displayMonth) && "text-muted-foreground/40",
              isSameDay(day, selected) && "bg-primary text-primary-foreground",
              !isSameDay(day, selected) && isToday(day) && "font-semibold text-primary",
              !isSameDay(day, selected) && "hover:bg-muted",
            )}
          >
            {day.getDate()}
          </button>
        ))}
      </div>
    </div>
  );
}

export function DateTimeField({
  id, value, onChange, mode = "datetime", disabled, tz, onValidityChange,
}: DateTimeFieldProps) {
  const allDay = mode === "date";
  const isMobile = useIsMobile();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(() => displayValue(value, allDay));
  const [invalid, setInvalid] = useState(false);
  const fallbackId = useId();
  const inputId = id ?? fallbackId;

  // Resync the displayed text whenever the field's own value changes from
  // outside -- the initial load, the all-day toggle, or this field's own
  // successful parse landing back through the parent's state. Never while
  // typing invalid text: this effect only reruns when `value` itself
  // changes, which an unparsed edit never does, so nothing here silently
  // reverts a mistake the person can still see and fix.
  useEffect(() => {
    setText(displayValue(value, allDay));
    setInvalid(false);
    onValidityChange?.(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, allDay]);

  const apply = (date: Date) => {
    const iso = allDay ? wholeDayIso(format(date, "yyyy-MM-dd")) : date.toISOString();
    setInvalid(false);
    onValidityChange?.(true);
    onChange(iso);
  };

  const commitText = () => {
    // Read from `text` here, in the blur/Enter handler -- never inside a
    // later state updater, which runs during render, a render after the
    // control has moved on.
    const current = allDay ? wholeDayLocalDate(value) : new Date(value);
    const parsed = parseDateTimeInput(text, current, new Date(), allDay);
    if (parsed === null) {
      setInvalid(true);
      onValidityChange?.(false);
      return;
    }
    apply(parsed);
  };

  const handleDaySelect = (day: Date) => {
    const current = allDay ? wholeDayLocalDate(value) : new Date(value);
    apply(combineDatePart(day, current, allDay));
    if (allDay) setOpen(false);
  };

  const handleTimeChange = (hour: number, minute: number) => {
    const merged = new Date(value);
    merged.setHours(hour, minute, 0, 0);
    apply(merged);
  };

  const current = allDay ? wholeDayLocalDate(value) : new Date(value);
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const showZoneCaption = !allDay && !!tz && tz !== browserZone;

  const picker = (
    <div className="flex flex-col gap-3 p-1">
      <MiniCalendar selected={current} onSelect={handleDaySelect} />
      {!allDay && (
        <div className="flex items-center gap-2">
          <label className="grid gap-1 text-xs text-muted-foreground">
            HH
            <Input
              type="number" min={0} max={23} value={current.getHours()}
              className="w-14"
              onChange={(e) => handleTimeChange(Number(e.target.value), current.getMinutes())}
            />
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            MM
            <Input
              type="number" min={0} max={59} value={current.getMinutes()}
              className="w-14"
              onChange={(e) => handleTimeChange(current.getHours(), Number(e.target.value))}
            />
          </label>
        </div>
      )}
    </div>
  );

  const textField = (
    <Input
      id={inputId}
      value={text}
      disabled={disabled}
      aria-invalid={invalid}
      onChange={(e) => setText(e.target.value)}
      onBlur={commitText}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commitText();
        }
      }}
    />
  );

  return (
    <div className="grid gap-1">
      <div className="flex items-center gap-1">
        {textField}
        {isMobile ? (
          <>
            <Button
              type="button" variant="outline" size="icon" aria-label="Choose a date"
              disabled={disabled} onClick={() => setOpen(true)}
            >
              <CalendarIcon className="h-4 w-4" />
            </Button>
            <Sheet open={open} onOpenChange={setOpen}>
              <SheetContent side="bottom">
                <SheetHeader>
                  <SheetTitle>{allDay ? "Choose a date" : "Choose a date and time"}</SheetTitle>
                </SheetHeader>
                <div className="px-4 pb-4">{picker}</div>
              </SheetContent>
            </Sheet>
          </>
        ) : (
          <Popover open={open} onOpenChange={setOpen}>
            <PopoverTrigger
              render={
                <Button
                  type="button" variant="outline" size="icon" aria-label="Choose a date"
                  disabled={disabled}
                />
              }
            >
              <CalendarIcon className="h-4 w-4" />
            </PopoverTrigger>
            <PopoverContent className="w-64" align="end">
              {picker}
            </PopoverContent>
          </Popover>
        )}
      </div>
      {invalid && (
        <p className="text-xs text-destructive">
          Not a date{allDay ? "" : " and time"} this understands.
        </p>
      )}
      {showZoneCaption && <p className="text-xs text-muted-foreground">{tz}</p>}
    </div>
  );
}
