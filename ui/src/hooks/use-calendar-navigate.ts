"use client";

/**
 * The one function that writes the calendar's URL -- every other write
 * (a raw router.replace competing with this hook's own push, in
 * particular) raced it and Next's router resolved the race by silently
 * downgrading the push to a replace, so the back button had nothing to
 * return to. Every caller, explicit navigation and passive write-back
 * alike, goes through here now; `push: false` is what the month
 * scroller's own scroll-driven date update uses, and what continuous
 * stepping (prev/next, the day/week keyboard shortcuts) uses too, so
 * neither spends a history entry on every unit stepped through.
 */

import { useCallback, useRef } from "react";
import { useAtom, useAtomValue } from "jotai";
import { useRouter } from "next/navigation";
import { calendarDateAtom, calendarViewAtom, type CalendarViewMode } from "@/lib/atoms";
import { isoDate } from "@/lib/dates";

/**
 * `view`/`date` are read from refs, not from the callback's own closure,
 * so the returned function has one stable identity for the component's
 * whole lifetime rather than a new one every time either atom changes --
 * which, for `date`, is on every row the month scroller scrolls past. A
 * churning `navigate` identity cascades into every caller that builds a
 * handler with `[navigate]` as a dependency (onSelectDay, onSelectWeek in
 * calendar-page.tsx), and from there into the props `MonthWeekRow`
 * receives -- silently defeating its memoization on every row crossed,
 * regardless of how stable everything else about it is. */
export function useCalendarNavigate() {
  const router = useRouter();
  const [view, setView] = useAtom(calendarViewAtom);
  const [date, setDate] = useAtom(calendarDateAtom);
  const viewRef = useRef(view);
  viewRef.current = view;
  const dateRef = useRef(date);
  dateRef.current = date;

  return useCallback(
    (next: { view?: CalendarViewMode; date?: Date }, options?: { push?: boolean }) => {
      const nextView = next.view ?? viewRef.current;
      const nextDate = next.date ?? dateRef.current;
      if (next.view !== undefined) setView(next.view);
      if (next.date !== undefined) setDate(next.date);
      const params = new URLSearchParams({ view: nextView, date: isoDate(nextDate) });
      const url = `/calendar?${params.toString()}`;
      if (options?.push === false) router.replace(url, { scroll: false });
      else router.push(url, { scroll: false });
    },
    [setView, setDate, router],
  );
}

/**
 * Writes the URL from whatever the view/date atoms currently hold, without
 * touching either -- for a caller that already keeps them current itself
 * (the month scroller writes `calendarDateAtom` directly, cheaply, on
 * every row scrolled past) and only wants the address bar to catch up
 * once, when scrolling settles. `router.replace` is what every other
 * passive write here uses too: a settle event happens once per pause, not
 * once per user gesture, so it never deserves a history entry. */
export function useCalendarUrlWriter() {
  const router = useRouter();
  const view = useAtomValue(calendarViewAtom);
  const date = useAtomValue(calendarDateAtom);
  return useCallback(() => {
    const params = new URLSearchParams({ view, date: isoDate(date) });
    router.replace(`/calendar?${params.toString()}`, { scroll: false });
  }, [view, date, router]);
}
