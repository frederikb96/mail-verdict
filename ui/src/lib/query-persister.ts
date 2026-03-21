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

export function isEphemeralQuery(queryKey: readonly unknown[]): boolean {
  const first = queryKey[0];
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
