/** Calendar colour palette and the CSS custom-property values derived from it. */

import type { CSSProperties } from "react";
import type { Calendar } from "@/types/api";

/** Twelve hues, assigned round-robin to calendars that have never picked one
 * themselves (a Nextcloud collection usually has its own colour already). */
export const CALENDAR_PALETTE = [
  "#3b82f6", // blue
  "#22c55e", // green
  "#f97316", // orange
  "#a855f7", // purple
  "#ec4899", // pink
  "#14b8a6", // teal
  "#eab308", // yellow
  "#ef4444", // red
  "#6366f1", // indigo
  "#84cc16", // lime
  "#06b6d4", // cyan
  "#f43f5e", // rose
] as const;

export function paletteColorForIndex(index: number): string {
  return CALENDAR_PALETTE[index % CALENDAR_PALETTE.length];
}

/** A stable palette index for a calendar with no colour of its own --
 * keyed by its id rather than its position in whatever list it currently
 * renders in, so the same calendar keeps the same colour across a reload
 * or a reorder. A plain string hash (djb2), not cryptographic, only
 * needs to spread ids evenly across twelve buckets. */
function paletteColorForCalendarId(id: string): string {
  let hash = 5381;
  for (let i = 0; i < id.length; i++) {
    hash = (hash * 33 + id.charCodeAt(i)) | 0;
  }
  return paletteColorForIndex(Math.abs(hash));
}

/** The colour a calendar renders with everywhere -- a user override wins,
 * then the server's own colour. A CalDAV collection with neither (a
 * freshly created one commonly has none) falls back to a palette colour
 * derived from the calendar's own id, so it is never left to render as
 * transparent or indistinguishable from every other uncoloured
 * calendar. */
export function resolveCalendarColor(
  calendar: Pick<Calendar, "id" | "color" | "color_override">,
): string {
  return calendar.color_override || calendar.color || paletteColorForCalendarId(calendar.id);
}

/** Sets `--cal-color` and the derived surface variables an event chip reads.
 * `oklch(from ...)` relative colour syntax (Baseline 2024) darkens for text
 * in light mode and lightens in dark mode from the one source colour. */
export function calendarColorStyle(color: string): CSSProperties {
  return { "--cal-color": color } as CSSProperties;
}
