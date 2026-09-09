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

import { useCallback } from "react";
import { useSetAtom, useStore } from "jotai";
import { useRouter } from "next/navigation";
import { calendarDateAtom, calendarViewAtom, type CalendarViewMode } from "@/lib/atoms";
import { isoDate } from "@/lib/dates";

/**
 * `view`/`date` are read from the jotai store at call time, never
 * subscribed to: this hook is mounted by the calendar page itself and by
 * every control on it, and `calendarDateAtom` changes on every row the
 * month scroller scrolls past, so subscribing here would re-render all of
 * them per row crossed and hand the returned function a new identity each
 * time -- which cascades into every handler built with `[navigate]` as a
 * dependency and from there into the props `MonthWeekRow` receives,
 * silently defeating its memoization. Reading from the store gives one
 * stable function for the component's whole lifetime and no re-render. */
export function useCalendarNavigate() {
  const router = useRouter();
  const store = useStore();
  const setView = useSetAtom(calendarViewAtom);
  const setDate = useSetAtom(calendarDateAtom);

  return useCallback(
    (next: { view?: CalendarViewMode; date?: Date }, options?: { push?: boolean }) => {
      const nextView = next.view ?? store.get(calendarViewAtom);
      const nextDate = next.date ?? store.get(calendarDateAtom);
      if (next.view !== undefined) setView(next.view);
      if (next.date !== undefined) setDate(next.date);
      const params = new URLSearchParams({ view: nextView, date: isoDate(nextDate) });
      const url = `/calendar?${params.toString()}`;
      if (options?.push === false) router.replace(url, { scroll: false });
      else router.push(url, { scroll: false });
    },
    [store, setView, setDate, router],
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
  const store = useStore();
  return useCallback(() => {
    const params = new URLSearchParams({
      view: store.get(calendarViewAtom), date: isoDate(store.get(calendarDateAtom)),
    });
    router.replace(`/calendar?${params.toString()}`, { scroll: false });
  }, [store, router]);
}
