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

/**
 * A one-shot signal: the message a fresh mail-list view should centre its
 * first page on, rather than starting at the newest edge -- set alongside
 * selectedMailIdAtom when a message is opened from somewhere that doesn't
 * know its place in the ordinary newest-first window (search, currently
 * the only such entry point). mail-list.tsx captures it once per list
 * identity and clears it immediately after, so it never survives past
 * the list it was meant for.
 *
 * threadId travels with it for the reveal step: in threaded mode the
 * server resolves `around` to the target's *thread's* representative
 * row, a different id from the message itself, so finding which loaded
 * row to scroll to needs the thread id, not the message id, to match on.
 */
export const pendingAroundMailIdAtom = atom<{ id: string; threadId: string } | null>(null);

/**
 * The most recent mail.new SSE arrival, by account/folder -- not a log,
 * just the latest one, since a mail-list watching for "is there something
 * newer than my own window" only needs to notice that the value changed,
 * not replay every arrival it missed while unmounted. A window that is
 * not at the newest edge (see find.md's non-tail obligations) must not
 * append a live arrival into itself -- this is what lets it count how
 * many it is missing instead, without holding a second subscription to
 * the event stream.
 */
export const mailArrivedAtom = atom<{
  accountId: string;
  folderId: string;
  messageId: string;
} | null>(null);

/** The thread id a reply/forward is currently in progress against, while
 * that reply has unsaved content -- null otherwise. useMailAction reads
 * this to decide whether a "leaves folder" action (trash, archive, move,
 * spam) taken on a message in that same thread, from somewhere else (a
 * row's own hover control, a keyboard shortcut), may still clear the
 * selection: doing so unmounts the reading pane, and with it the reply
 * box, discarding whatever was typed with no prompt at all.
 *
 * Keyed by thread id rather than by the single message either side would
 * otherwise have to agree on: a reply always targets the thread's newest
 * message, while the reading pane's own "open" message can be an older
 * one the reader expanded within the same thread -- matching on the
 * message alone would still let trashing that older one discard a reply
 * against the newest. */
export const activeReplyDirtyForThreadIdAtom = atom<string | null>(null);

/** A pending selection that a dirty composer is currently holding up -- set
 * by requestSelectMailAtom below instead of writing selectedMailIdAtom
 * directly, and resolved by whichever composer is dirty (save, discard, or
 * cancel) writing selectedMailIdAtom itself once it is done, or clearing
 * this back to null. */
export const blockedMailSelectionAtom = atom<string | null>(null);

/** The only way to open a message, close the reading pane, or otherwise
 * change which message is selected. Write-only: it consults whatever
 * composer is dirty before letting the selection move, so the guard lives
 * once here rather than being re-derived at every call site -- a dirty
 * reply already discarded itself silently through more than one such site
 * before this existed, and the next site added would not have known to
 * ask either.
 *
 * A single-row action that removes the open message from its own folder
 * (trash, archive, spam, move) is a different question -- whether that
 * message's own thread has a reply in progress, which useMailAction
 * already answers itself -- and does not go through this atom. */
export const requestSelectMailAtom = atom(null, (get, set, next: string | null) => {
  if (get(activeReplyDirtyForThreadIdAtom) !== null) {
    set(blockedMailSelectionAtom, next);
    return;
  }
  set(selectedMailIdAtom, next);
});

/** Whether the mail list groups messages into conversations. Defaults on. */
export const threadedViewAtom = atomWithStorage<boolean>(
  "mailverdict:threaded",
  true,
);

/** A compose dialog can be asked to open from anywhere (a contact's email,
 * an event's "email the attendees", a `mailto:` link the browser routes here)
 * without owning its own trigger. */
export const composeIntentAtom = atom<{
  accountId?: string;
  to?: string[];
  cc?: string[];
  bcc?: string[];
  subject?: string;
  bodyHtml?: string;
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

/** The message someone explicitly marked unread, whichever surface they used
 * -- a row's hover control, the reading pane's own button, a keyboard
 * shortcut. The reading pane's auto-mark-read effect consults it so that its
 * reaction to the unread flip does not immediately undo the action; it is
 * cleared once a different message is open, so reopening the original later
 * is a fresh look at it. One writer, one reader: a marker kept per surface
 * covers only the surfaces that remembered to set it. */
export const explicitlyUnreadMailIdAtom = atom<string | null>(null);
