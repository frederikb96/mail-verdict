/**
 * Keyboard shortcuts for the calendar page.
 *
 * t: today  d/w/m/a: switch view  n: new event  j/k or arrows: next/previous
 * period  Escape: close the popover  Delete: open the confirm for the
 * selected event.
 */

"use client";

import { useEffect } from "react";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { calendarDateAtom, calendarViewAtom, eventDeleteRequestAtom, selectedEventAtom } from "@/lib/atoms";
import { addDays, addMonths, addWeeks } from "@/lib/dates";
import { isEditableElement } from "@/lib/utils";
import { useCalendarNavigate } from "@/hooks/use-calendar-navigate";

interface UseCalendarShortcutsOptions {
  /** Called on "n" to open the create-event editor. */
  onCreate?: () => void;
}

export function useCalendarShortcuts({ onCreate }: UseCalendarShortcutsOptions = {}) {
  const view = useAtomValue(calendarViewAtom);
  const date = useAtomValue(calendarDateAtom);
  const navigate = useCalendarNavigate();
  const [selected, setSelected] = useAtom(selectedEventAtom);
  const setDeleteRequest = useSetAtom(eventDeleteRequestAtom);

  useEffect(() => {
    // push: false -- j/k held down or repeated must not fill the
    // back-button history with one entry per period stepped through.
    function step(dir: 1 | -1) {
      if (view === "day") navigate({ date: addDays(date, dir) }, { push: false });
      else if (view === "week") navigate({ date: addWeeks(date, dir) }, { push: false });
      else navigate({ date: addMonths(date, dir) }, { push: false });
    }

    function handleKeyDown(e: KeyboardEvent) {
      if (isEditableElement(e.target)) return;

      switch (e.key) {
        case "t":
          navigate({ date: new Date() });
          break;
        case "d":
          navigate({ view: "day" });
          break;
        case "w":
          navigate({ view: "week" });
          break;
        case "m":
          navigate({ view: "month" });
          break;
        case "a":
          navigate({ view: "agenda" });
          break;
        case "n":
          onCreate?.();
          break;
        case "j":
        case "ArrowRight":
          step(1);
          break;
        case "k":
        case "ArrowLeft":
          step(-1);
          break;
        case "Escape":
          setSelected(null);
          break;
        case "Delete":
        case "Backspace":
          if (selected) setDeleteRequest({ ...selected });
          break;
        default:
          return;
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [view, date, selected, navigate, setSelected, setDeleteRequest, onCreate]);
}
