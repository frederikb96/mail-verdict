"use client";

/**
 * Reads the URL into the atoms -- on mount (a deep link, a reload) and
 * whenever it changes without going through use-calendar-navigate.ts (the
 * browser's back/forward buttons). Writing the URL is deliberately not
 * this hook's job: use-calendar-navigate.ts is the one function that does
 * that, for the same reason a scroll position needs exactly one writer --
 * this hook's own write effect and a navigate() push firing for the same
 * change raced, and Next's router resolved the pair to a replace, so
 * every push silently became a replace and the back button had nothing
 * to return to. A bare `/calendar` (first visit, no params) is populated
 * once here, the only write this hook does, and it happens before any
 * navigate() call could be competing with it.
 */

import { useEffect, useRef } from "react";
import { useAtom } from "jotai";
import { useRouter, useSearchParams } from "next/navigation";
import { calendarDateAtom, calendarViewAtom, type CalendarViewMode } from "@/lib/atoms";
import { isoDate } from "@/lib/dates";

const VALID_VIEWS: CalendarViewMode[] = ["day", "week", "month", "agenda"];

function parseDateParam(value: string | null): Date | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const parsed = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function useCalendarUrlSync(): void {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [view, setView] = useAtom(calendarViewAtom);
  const [date, setDate] = useAtom(calendarDateAtom);
  const populatedRef = useRef(false);

  useEffect(() => {
    const paramView = searchParams.get("view");
    const paramDate = parseDateParam(searchParams.get("date"));

    if (!paramView && !paramDate) {
      if (populatedRef.current) return;
      populatedRef.current = true;
      const params = new URLSearchParams({ view, date: isoDate(date) });
      router.replace(`/calendar?${params.toString()}`, { scroll: false });
      return;
    }
    populatedRef.current = true;

    if (paramView && VALID_VIEWS.includes(paramView as CalendarViewMode) && paramView !== view) {
      setView(paramView as CalendarViewMode);
    }
    if (paramDate && paramDate.getTime() !== date.getTime()) {
      setDate(paramDate);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);
}
