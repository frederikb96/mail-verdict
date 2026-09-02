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

// --- Contacts ---

export const selectedContactIdAtom = atom<string | null>(null);
