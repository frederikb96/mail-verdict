/** The calendar settings category (settings.calendar in the database) --
 * currently just the default duration a click or a drag creates an event
 * with, which also doubles as the grid's own snap granularity. Settings
 * are untyped JSONB on the wire, the same as every other category. */

import { useSettings } from "@/hooks/use-settings";

/** Matches settings/defaults.py's own default -- used while the settings
 * query hasn't resolved yet, never as a silent fallback for a value that
 * came back and was simply absent (which would be a server bug worth
 * seeing, not hiding). */
const FALLBACK_DURATION_MINUTES = 30;

export function useDefaultEventDurationMinutes(): number {
  const { data } = useSettings("calendar");
  const value = data?.default_event_duration_minutes;
  return typeof value === "number" && value > 0 ? value : FALLBACK_DURATION_MINUTES;
}

/** The calendar a new event's editor opens on when nothing more specific
 * names one (an edit's own event, or a click-to-create's own day/slot,
 * both still win). "" while unset or unresolved -- never undefined, the
 * same rule an initial Select value always needs: a control that starts
 * undefined is permanently uncontrolled once a real value arrives. */
export function useDefaultCalendarId(): string {
  const { data } = useSettings("calendar");
  const value = data?.default_calendar_id;
  return typeof value === "string" ? value : "";
}
