/**
 * Keyboard shortcuts for the calendar page: the rebindable actions of the
 * "calendar" scope in lib/shortcuts.ts (defaults: t today, d/w/m/a views,
 * n new event, j/k next/previous period), plus the fixed keys -- arrows
 * for the period, Escape to close the popover, Delete to open the confirm
 * for the selected event. None of them act from a text field or a dialog,
 * and the fixed ones only as bare presses, so Ctrl+D or Ctrl+A stay the
 * browser's.
 */

"use client";

import { useEffect } from "react";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { calendarDateAtom, calendarViewAtom, eventDeleteRequestAtom, selectedEventAtom } from "@/lib/atoms";
import { addDays, addMonths, addWeeks } from "@/lib/dates";
import { isEditableElement } from "@/lib/utils";
import { useShortcutBindings } from "@/lib/shortcut-bindings";
import { matchAction } from "@/lib/shortcuts";
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
  const bindings = useShortcutBindings();

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
      if (e.target instanceof Element && e.target.closest('[role="dialog"], [role="alertdialog"]')) return;

      const bare = !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey;
      switch (bare ? e.key : null) {
        case "ArrowRight":
          step(1);
          return;
        case "ArrowLeft":
          step(-1);
          return;
        case "Escape":
          setSelected(null);
          return;
        case "Delete":
        case "Backspace":
          if (selected) setDeleteRequest({ ...selected });
          return;
      }

      switch (matchAction(e, bindings, "calendar")) {
        case "calendar_today":
          navigate({ date: new Date() });
          break;
        case "calendar_day":
          navigate({ view: "day" });
          break;
        case "calendar_week":
          navigate({ view: "week" });
          break;
        case "calendar_month":
          navigate({ view: "month" });
          break;
        case "calendar_agenda":
          navigate({ view: "agenda" });
          break;
        case "calendar_new_event":
          onCreate?.();
          break;
        case "calendar_next":
          step(1);
          break;
        case "calendar_previous":
          step(-1);
          break;
        default:
          return;
      }
      e.preventDefault();
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [view, date, selected, navigate, setSelected, setDeleteRequest, onCreate, bindings]);
}
