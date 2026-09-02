/**
 * Keyboard shortcuts for the calendar page.
 *
 * t: today  d/w/m/a: switch view  n: new event  j/k or arrows: next/previous
 * period  Escape: close the popover  Delete: open the confirm for the
 * selected event.
 */

"use client";

import { useEffect } from "react";
import { useAtom, useSetAtom } from "jotai";
import {
  calendarDateAtom,
  calendarViewAtom,
  selectedEventAtom,
  type CalendarViewMode,
} from "@/lib/atoms";
import { addDays, addMonths, addWeeks } from "@/lib/dates";
import { isEditableElement } from "@/lib/utils";

interface UseCalendarShortcutsOptions {
  /** Called on "n" to open the create-event editor. */
  onCreate?: () => void;
  /** Called on Delete/Backspace with an event selected. */
  onRequestDelete?: (objectId: string, recurrenceId: string | null) => void;
}

export function useCalendarShortcuts({ onCreate, onRequestDelete }: UseCalendarShortcutsOptions = {}) {
  const [view, setView] = useAtom(calendarViewAtom);
  const setDate = useSetAtom(calendarDateAtom);
  const [selected, setSelected] = useAtom(selectedEventAtom);

  useEffect(() => {
    function step(dir: 1 | -1) {
      if (view === "day") setDate((d) => addDays(d, dir));
      else if (view === "week") setDate((d) => addWeeks(d, dir));
      else setDate((d) => addMonths(d, dir));
    }

    function handleKeyDown(e: KeyboardEvent) {
      if (isEditableElement(e.target)) return;

      switch (e.key) {
        case "t":
          setDate(new Date());
          break;
        case "d":
          setView("day" as CalendarViewMode);
          break;
        case "w":
          setView("week" as CalendarViewMode);
          break;
        case "m":
          setView("month" as CalendarViewMode);
          break;
        case "a":
          setView("agenda" as CalendarViewMode);
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
          if (selected) onRequestDelete?.(selected.objectId, selected.recurrenceId);
          break;
        default:
          return;
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [view, selected, setDate, setView, setSelected, onCreate, onRequestDelete]);
}
