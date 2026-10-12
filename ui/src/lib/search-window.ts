/**
 * The search time window: which stretch of received_at a search covers.
 *
 * Presets are stored as the preset and resolved against the clock each time
 * a query runs, so "Last 7 days" stays rolling. The slider and the custom
 * form store whole days (`YYYY-MM-DD`, local calendar days); a slider window
 * with no end runs up to the moment of the query.
 *
 * Pure date arithmetic only, so it can be unit tested without a DOM.
 */

export type WindowPreset = "today" | "since-yesterday" | "last-7-days" | "last-30-days";

export type SearchWindow =
  | { kind: "preset"; preset: WindowPreset }
  /** `until: null` runs to now. */
  | { kind: "slider"; since: string; until: string | null }
  | { kind: "custom"; since: string; until: string };

export const WINDOW_PRESETS: { preset: WindowPreset; label: string }[] = [
  { preset: "today", label: "Today" },
  { preset: "since-yesterday", label: "Since yesterday" },
  { preset: "last-7-days", label: "Last 7 days" },
  { preset: "last-30-days", label: "Last 30 days" },
];

/** The slider's axis: the last 52 weeks, one stop per week (stops 0..52). */
export const SLIDER_WEEKS = 52;

const HOUR_MS = 3_600_000;

/** Local calendar day as `YYYY-MM-DD` -- ISO order whatever the locale. */
export function formatDay(date: Date): string {
  const y = String(date.getFullYear()).padStart(4, "0");
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** Local midnight of a `YYYY-MM-DD` string, or null when it is not a real date. */
export function parseDay(text: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text.trim());
  if (!match) return null;
  const [y, m, d] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const date = new Date(y, m - 1, d);
  // Rejects 2026-02-31 and the like, which Date silently rolls over.
  if (date.getFullYear() !== y || date.getMonth() !== m - 1 || date.getDate() !== d) return null;
  return date;
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function endOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate(), 23, 59, 59, 999);
}

function addDays(date: Date, days: number): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
}

/** Whole calendar days from `from` to `to` (DST-safe). */
function daysBetween(from: Date, to: Date): number {
  const a = Date.UTC(from.getFullYear(), from.getMonth(), from.getDate());
  const b = Date.UTC(to.getFullYear(), to.getMonth(), to.getDate());
  return Math.round((b - a) / (24 * HOUR_MS));
}

/** Local midnight of the slider stop `p` (0..SLIDER_WEEKS). */
export function sliderStopDate(p: number, now: Date): Date {
  return addDays(startOfDay(now), -(SLIDER_WEEKS - p) * 7);
}

/** The window a slider selection `[a, b]` (a < b) stands for. */
export function sliderWindow(a: number, b: number, now: Date): SearchWindow {
  return {
    kind: "slider",
    since: formatDay(sliderStopDate(a, now)),
    until: b >= SLIDER_WEEKS ? null : formatDay(sliderStopDate(b, now)),
  };
}

/** The slider stops a window sits on: its own for a slider window, the
 * full axis for anything else (opening the slider then means "the last 52
 * weeks"). */
export function sliderStops(window: SearchWindow | null, now: Date): [number, number] {
  if (window?.kind !== "slider") return [0, SLIDER_WEEKS];
  const today = startOfDay(now);
  const stopOf = (day: string): number => {
    const date = parseDay(day);
    if (!date) return 0;
    return SLIDER_WEEKS - Math.round(daysBetween(date, today) / 7);
  };
  const a = Math.min(SLIDER_WEEKS - 1, Math.max(0, stopOf(window.since)));
  const b = window.until === null ? SLIDER_WEEKS : stopOf(window.until);
  return [a, Math.min(SLIDER_WEEKS, Math.max(a + 1, b))];
}

export interface MonthTick {
  stop: number;
  /** "Jan 2026" at the first tick and at each January, the short month name otherwise. */
  label: string;
  /** Carries the year, so its label is the wider one and is placed first. */
  isYear: boolean;
}

/** One tick per month along the slider axis, at the stop of the month's
 * first week. The first tick and every January carry the year, so the axis
 * orients by year. */
export function monthTicks(now: Date): MonthTick[] {
  const ticks: MonthTick[] = [];
  for (let p = 0; p <= SLIDER_WEEKS; p++) {
    const date = sliderStopDate(p, now);
    if (date.getDate() <= 7) {
      const month = date.toLocaleString("en", { month: "short" });
      const isYear = ticks.length === 0 || date.getMonth() === 0;
      ticks.push({
        stop: p,
        label: isYear ? `${month} ${date.getFullYear()}` : month,
        isYear,
      });
    }
  }
  return ticks;
}

const YEAR_LABEL_PX = 60;
const MONTH_LABEL_PX = 26;
const LABEL_GAP_PX = 8;

/** The ticks of `ticks` whose labels fit on an axis `widthPx` wide without
 * touching: year labels claim their place first, months fill what is left.
 * Every tick keeps its mark; only the text is dropped. */
export function labelledTicks(ticks: MonthTick[], widthPx: number): Set<number> {
  const shown: { x: number; half: number }[] = [];
  const stops = new Set<number>();
  const byPriority = [...ticks.filter((t) => t.isYear), ...ticks.filter((t) => !t.isYear)];
  for (const tick of byPriority) {
    const x = (tick.stop / SLIDER_WEEKS) * widthPx;
    const half = (tick.isYear ? YEAR_LABEL_PX : MONTH_LABEL_PX) / 2;
    if (shown.every((s) => Math.abs(s.x - x) >= s.half + half + LABEL_GAP_PX)) {
      shown.push({ x, half });
      stops.add(tick.stop);
    }
  }
  return stops;
}

/** The instants a window stands for right now, as the ISO strings both
 * search endpoints take as received_after / received_before. */
export function resolveWindow(
  window: SearchWindow,
  now: Date,
): { after: string; before: string } {
  const today = startOfDay(now);
  const nowIso = now.toISOString();
  switch (window.kind) {
    case "preset":
      switch (window.preset) {
        case "today":
          return { after: today.toISOString(), before: nowIso };
        case "since-yesterday":
          return { after: addDays(today, -1).toISOString(), before: nowIso };
        case "last-7-days":
          return { after: new Date(now.getTime() - 7 * 24 * HOUR_MS).toISOString(), before: nowIso };
        case "last-30-days":
          return { after: new Date(now.getTime() - 30 * 24 * HOUR_MS).toISOString(), before: nowIso };
      }
      break;
    case "slider":
    case "custom": {
      const since = parseDay(window.since) ?? today;
      const until = window.until === null ? null : parseDay(window.until);
      return {
        after: startOfDay(since).toISOString(),
        before: until ? endOfDay(until).toISOString() : nowIso,
      };
    }
  }
}

/** The label for the clock button's tooltip and the active-window chip. */
export function windowLabel(window: SearchWindow): string {
  if (window.kind === "preset") {
    return WINDOW_PRESETS.find((p) => p.preset === window.preset)?.label ?? window.preset;
  }
  return `${window.since} – ${window.until ?? "now"}`;
}

/** A stable string for query keys and list identities. */
export function windowKey(window: SearchWindow | null): string {
  return window ? JSON.stringify(window) : "";
}
