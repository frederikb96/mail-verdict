"use client";

/**
 * Reads the URL into the atoms -- on mount (a deep link, a reload) and on
 * a back/forward navigation. Writing the URL is deliberately not this
 * hook's job: use-calendar-navigate.ts is the one place that does that,
 * for the same reason a scroll position needs exactly one writer -- this
 * hook's own write effect and a navigate() push firing for the same
 * change raced, and Next's router resolved the pair to a replace, so
 * every push silently became a replace and the back button had nothing
 * to return to. A bare `/calendar` (first visit, no params) is populated
 * once here, the only write this hook does, and it happens before any
 * navigate() call could be competing with it.
 *
 * Why mount and `popstate`, rather than reacting to every searchParams
 * change: every URL this app writes comes back through the router as a
 * searchParams change too, indistinguishable from a navigation -- and it
 * comes back late, after the write's own re-render cost, while the month
 * scroller has kept scrolling. Applying that echo dragged the view back
 * to wherever it last paused, and under load several writes are in
 * flight at once, so no record of "the URL we last wrote" tells the
 * echoes apart from a real navigation reliably. `popstate` is the one
 * URL change that is not ours. An in-app link to `/calendar` with its
 * own params while this page is already mounted would therefore not be
 * applied -- every in-app calendar navigation goes through navigate().
 */

import { useCallback, useEffect, useRef } from "react";
import { useSetAtom, useStore } from "jotai";
import { useRouter, useSearchParams } from "next/navigation";
import { calendarDateAtom, calendarViewAtom, type CalendarViewMode } from "@/lib/atoms";
import { calendarUrl, parseCalendarDate } from "@/lib/calendar-url";

const VALID_VIEWS: CalendarViewMode[] = ["day", "week", "month", "agenda"];

export function useCalendarUrlSync(): void {
  const router = useRouter();
  const searchParams = useSearchParams();
  // The atoms are read from the store rather than subscribed to: this hook
  // sits at the top of the calendar page, and calendarDateAtom changes on
  // every row the month scroller scrolls past.
  const store = useStore();
  const setView = useSetAtom(calendarViewAtom);
  const setDate = useSetAtom(calendarDateAtom);
  const readRef = useRef(false);

  const apply = useCallback((params: URLSearchParams) => {
    const paramView = params.get("view");
    const paramDate = parseCalendarDate(params.get("date"));
    if (
      paramView && VALID_VIEWS.includes(paramView as CalendarViewMode)
      && paramView !== store.get(calendarViewAtom)
    ) {
      setView(paramView as CalendarViewMode);
    }
    if (paramDate && paramDate.getTime() !== store.get(calendarDateAtom).getTime()) {
      setDate(paramDate);
    }
  }, [store, setView, setDate]);

  // The mount read, once. Keyed on searchParams rather than run bare so it
  // sees the real URL's parameters and not the static export's empty
  // prerender ones, whichever render they first arrive in.
  useEffect(() => {
    if (readRef.current) return;
    readRef.current = true;
    if (!searchParams.get("view") && !searchParams.get("date")) {
      const url = calendarUrl(store.get(calendarViewAtom), store.get(calendarDateAtom));
      router.replace(url, { scroll: false });
      return;
    }
    apply(searchParams);
  }, [searchParams, apply, router, store]);

  useEffect(() => {
    function onPopState() {
      // Leaving the calendar route entirely is Next's to handle; this
      // fires before that unmount lands.
      if (!window.location.pathname.startsWith("/calendar")) return;
      apply(new URLSearchParams(window.location.search));
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [apply]);
}
