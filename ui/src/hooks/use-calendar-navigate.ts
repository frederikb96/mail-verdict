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
 *
 * Written through the History API, not router.push/replace -- the same
 * move use-mail-url-sync.ts made for the same reason: a route change
 * (`/calendar` itself) never happens here, only its query string, and
 * Next's router fetches the page's RSC payload for every push/replace
 * regardless, a request per click, falling back to a full page reload
 * after a deploy (a payload from a newer build). Next's own patch to
 * pushState/replaceState keeps useSearchParams in step with no fetch of
 * its own, which is what use-calendar-url-sync.ts's popstate listener
 * and mount read still rely on.
 */

import { useCallback } from "react";
import { useSetAtom, useStore } from "jotai";
import { calendarDateAtom, calendarViewAtom, type CalendarViewMode } from "@/lib/atoms";
import { calendarUrl } from "@/lib/calendar-url";

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
  const store = useStore();
  const setView = useSetAtom(calendarViewAtom);
  const setDate = useSetAtom(calendarDateAtom);

  return useCallback(
    (next: { view?: CalendarViewMode; date?: Date }, options?: { push?: boolean }) => {
      const nextView = next.view ?? store.get(calendarViewAtom);
      const nextDate = next.date ?? store.get(calendarDateAtom);
      if (next.view !== undefined) setView(next.view);
      if (next.date !== undefined) setDate(next.date);
      const url = calendarUrl(nextView, nextDate);
      if (options?.push === false) window.history.replaceState(null, "", url);
      else window.history.pushState(null, "", url);
    },
    [store, setView, setDate],
  );
}

/**
 * Writes the URL from whatever the view/date atoms currently hold, without
 * touching either -- for a caller that already keeps them current itself
 * (the month scroller writes `calendarDateAtom` directly, cheaply, on
 * every row scrolled past) and only wants the address bar to catch up
 * once, when scrolling settles. A replace, like every other passive write
 * here: a settle event happens once per pause, not once per user gesture,
 * so it never deserves a history entry. */
export function useCalendarUrlWriter() {
  const store = useStore();
  return useCallback(() => {
    window.history.replaceState(
      null, "", calendarUrl(store.get(calendarViewAtom), store.get(calendarDateAtom)),
    );
  }, [store]);
}
