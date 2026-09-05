/** Jotai state atoms. */

import { atom } from "jotai";
import { atomWithStorage } from "jotai/utils";

/**
 * Currently selected account ID.
 * Special value "unified" indicates the unified multi-account view.
 */
export const selectedAccountIdAtom = atom<string | null>(null);

/** Currently selected folder ID (single-account mode). */
export const selectedFolderIdAtom = atom<string | null>(null);

/** Currently selected unified folder name (unified view mode). */
export const selectedUnifiedFolderAtom = atom<string | null>(null);

/** Whether the unified view is active. */
export const isUnifiedViewAtom = atom<boolean>((get) => {
  return get(selectedAccountIdAtom) === "unified";
});

/** Currently selected mail ID */
export const selectedMailIdAtom = atom<string | null>(null);

/** The id of the message a reply/forward is currently in progress against,
 * while that reply has unsaved content -- null otherwise. useMailAction
 * reads this to decide whether a "leaves folder" action (trash, archive,
 * move, spam) taken on the open message from somewhere else (a row's own
 * hover control, a keyboard shortcut) may still clear the selection: doing
 * so unmounts the reading pane, and with it the reply box, discarding
 * whatever was typed with no prompt at all. Keyed by mail id rather than a
 * plain boolean since the write and the read happen in different
 * components that only agree on which message they mean. */
export const activeReplyDirtyForMailIdAtom = atom<string | null>(null);

/** Whether the mail list groups messages into conversations. Defaults on. */
export const threadedViewAtom = atomWithStorage<boolean>(
  "mailverdict:threaded",
  true,
);

/** A compose dialog can be asked to open from anywhere (a contact's email,
 * an event's "email the attendees") without owning its own trigger. */
export const composeIntentAtom = atom<{
  accountId?: string;
  to?: string[];
  subject?: string;
} | null>(null);

// --- Calendar ---

export type CalendarViewMode = "month" | "week" | "day" | "agenda";

/** Persisted like threadedViewAtom -- a view preference, not server state. */
export const calendarViewAtom = atomWithStorage<CalendarViewMode>(
  "mailverdict:calendar-view",
  "week",
);

/** The date the calendar is anchored on -- what "Today", the mini-month and
 * the toolbar arrows all resolve against. Read-write: the month scroller
 * writes it back as the reader scrolls, everyone else writes it to navigate. */
export const calendarDateAtom = atom<Date>(new Date());

/** The event the popover/editor is currently open for. */
export const selectedEventAtom = atom<{
  objectId: string;
  recurrenceId: string | null;
} | null>(null);

/** Where the quick-view popover anchors -- the clicked chip's bounding
 * rect, captured at click time since chips live in many different views. */
export const eventPopoverAnchorAtom = atom<DOMRect | null>(null);

/** A Delete/Backspace press asks the popover to open its own delete
 * confirmation for the selected event -- the key handler and the popover's
 * confirmation state are not siblings, so this carries the request across
 * without either owning the other. A fresh object each press so the same
 * event can be requested twice in a row. */
export const eventDeleteRequestAtom = atom<{
  objectId: string;
  recurrenceId: string | null;
} | null>(null);

/** The day/week time grid's vertical zoom -- a multiplier over its base
 * hour height. Persisted like the view mode: a display preference, not
 * server state. Ctrl+wheel is the only writer. */
export const calendarZoomAtom = atomWithStorage<number>("mailverdict:calendar-zoom", 1);

/** The hour-of-day (a decimal, e.g. 8.5 for 08:30) currently at the top
 * of the day/week grid's viewport -- an identity to restore by, per the
 * scrolling skill, rather than a remembered pixel offset that would be
 * meaningless at a different zoom. null until the grid has measured its
 * own first scroll position. */
export const calendarScrollHourAtom = atomWithStorage<number | null>(
  "mailverdict:calendar-scroll-hour", null,
);

// --- Contacts ---

export const selectedContactIdAtom = atom<string | null>(null);
