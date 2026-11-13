"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCalendars } from "@/hooks/use-calendars";

// The Select primitive treats an empty item value as "no selection", the
// same trap the event editor's own recurrence preset works around --
// "None" needs a value of its own on the wire, distinct from the id an
// actual calendar carries.
const NO_DEFAULT_CALENDAR = "none";

/** The one calendar setting that isn't a plain scalar a declared field
 * can draw: choosing a default calendar needs the
 * same enabled/writable calendar list the event editor itself offers,
 * not a raw id typed into a text box. `value` is always a string, never
 * undefined -- a Select decides on its first render whether it is
 * controlled from whether its value is undefined rather than merely
 * falsy, so a caller reading this from a query that hasn't resolved yet
 * must still pass "". */
export function DefaultCalendarSetting({
  value,
  onChange,
}: {
  value: string;
  /** The chosen calendar id, or null for none. */
  onChange: (calendarId: string | null) => void;
}) {
  const { data: calendars } = useCalendars();
  const writable = (calendars ?? []).filter((c) => !c.read_only && c.is_enabled);
  const itemValue = value || NO_DEFAULT_CALENDAR;

  return (
    <div>
      <Select
        value={itemValue}
        onValueChange={(v) => v && onChange(v === NO_DEFAULT_CALENDAR ? null : v)}
      >
        <SelectTrigger id="default-calendar-select" className="w-56">
          <SelectValue placeholder="None">
            {(v: string) =>
              v === NO_DEFAULT_CALENDAR ? "None" : (writable.find((c) => c.id === v)?.display_name ?? v)
            }
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NO_DEFAULT_CALENDAR}>None</SelectItem>
          {writable.map((c) => (
            <SelectItem key={c.id} value={c.id}>
              {c.display_name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
