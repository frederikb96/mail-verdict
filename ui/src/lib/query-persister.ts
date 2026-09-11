/**
 * TanStack Query localStorage persister.
 *
 * Persists query cache to localStorage for instant page loads.
 * Ephemeral queries (SSE connection, selection state) are excluded via
 * shouldDehydrateQuery in the persist options.
 */

import { createSyncStoragePersister } from "@tanstack/query-sync-storage-persister";

/** Queries matching these key prefixes are not persisted. */
const EPHEMERAL_PREFIXES = ["sse", "selection"];

/**
 * The mail list's own infinite queries -- ["mails", ...] for a single
 * account/folder, ["unified", "mails", ...] for the cross-account view --
 * grow without bound while a folder is scrolled: measured at roughly 615
 * bytes per message, a large folder crosses a typical browser storage quota
 * well before it finishes loading. The persister is built with no retry and
 * its own source swallows the resulting quota error with no log, so
 * persistence then stops silently and every later load restores whatever
 * snapshot last fit. The list refetches on mount anyway (staleTime 30s), so
 * persisting it buys almost nothing. Excluded by exact key shape rather than
 * by the "unified" prefix alone, which also covers the small unified folder
 * tree -- that one is worth persisting.
 */
export function isMailListQuery(queryKey: readonly unknown[]): boolean {
  return queryKey[0] === "mails" || (queryKey[0] === "unified" && queryKey[1] === "mails");
}

/**
 * Calendar-event month chunks -- `["calendar-events", "2026-09"]`, per
 * hooks/use-events.ts's eventKeys.chunk. Persisting these buys nothing:
 * SSE keeps them fresh (see use-sse.ts's calendar.object handling) and
 * they're cheap to re-fetch, but scrolling the month view can build up
 * years' worth of them, and every one is re-serialised into localStorage
 * on each throttled write -- the same unbounded-growth shape the mail list
 * is excluded for above, just discovered on the calendar side later.
 * Excluded by exact key shape rather than by prefix alone, so it doesn't
 * also catch `["calendar-event", id]` (singular -- the detail cache,
 * small and worth persisting) or `["calendars"]`/`["calendar-links"]`.
 */
function isCalendarEventChunkQuery(queryKey: readonly unknown[]): boolean {
  return queryKey[0] === "calendar-events";
}

export function isEphemeralQuery(queryKey: readonly unknown[]): boolean {
  const first = queryKey[0];
  if (isMailListQuery(queryKey) || isCalendarEventChunkQuery(queryKey)) return true;
  if (typeof first !== "string") return false;
  return EPHEMERAL_PREFIXES.some((p) => first.startsWith(p));
}

export const queryPersister =
  typeof window !== "undefined"
    ? createSyncStoragePersister({
        storage: window.localStorage,
        key: "mail-verdict-query-cache",
        throttleTime: 1000,
      })
    : null;

/** Max age for persisted cache entries (24 hours). */
export const PERSIST_MAX_AGE = 1000 * 60 * 60 * 24;
