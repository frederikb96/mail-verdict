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
import { useAtom } from "jotai";
import { useRouter } from "next/navigation";
import { calendarDateAtom, calendarViewAtom, type CalendarViewMode } from "@/lib/atoms";
import { isoDate } from "@/lib/dates";

export function useCalendarNavigate() {
  const router = useRouter();
  const [view, setView] = useAtom(calendarViewAtom);
  const [date, setDate] = useAtom(calendarDateAtom);

  return useCallback(
    (next: { view?: CalendarViewMode; date?: Date }, options?: { push?: boolean }) => {
      const nextView = next.view ?? view;
      const nextDate = next.date ?? date;
      if (next.view !== undefined) setView(next.view);
      if (next.date !== undefined) setDate(next.date);
      const params = new URLSearchParams({ view: nextView, date: isoDate(nextDate) });
      const url = `/calendar?${params.toString()}`;
      if (options?.push === false) router.replace(url, { scroll: false });
      else router.push(url, { scroll: false });
    },
    [view, date, setView, setDate, router],
  );
}
