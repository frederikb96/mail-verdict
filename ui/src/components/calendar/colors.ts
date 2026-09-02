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

/** The colour a calendar renders with everywhere -- a user override wins. */
export function resolveCalendarColor(calendar: Pick<Calendar, "color" | "color_override">): string {
  return calendar.color_override ?? calendar.color;
}

/** Sets `--cal-color` and the derived surface variables an event chip reads.
 * `oklch(from ...)` relative colour syntax (Baseline 2024) darkens for text
 * in light mode and lightens in dark mode from the one source colour. */
export function calendarColorStyle(color: string): CSSProperties {
  return { "--cal-color": color } as CSSProperties;
}
