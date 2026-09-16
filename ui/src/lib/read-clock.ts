/**
 * When the data a query holds was read -- the moment its request left, not
 * the moment the answer landed.
 *
 * The intent projection (mail-intents.ts) needs to know whether data could
 * already include an action, and a request answered after the action
 * succeeded but sent before it cannot. TanStack's own dataUpdatedAt is the
 * answer's time, and a page appended to an infinite query moves it for
 * every page loaded earlier, so it overstates freshness in exactly the
 * cases that bring hidden rows back. Every query the projection reads
 * records its read here instead.
 */

import { hashKey, type QueryKey } from "@tanstack/react-query";

const readAt = new Map<string, number>();

/**
 * Wrap a query function so a successful read records when it started.
 *
 * @param continues whether this fetch adds to data read earlier (a further
 *   page), which keeps the earlier, older read time
 */
export async function timedRead<T>(
  queryKey: QueryKey,
  signal: AbortSignal | undefined,
  continues: boolean,
  read: () => Promise<T>,
): Promise<T> {
  const startedAt = Date.now();
  const result = await read();
  if (!signal?.aborted) recordRead(queryKey, startedAt, continues);
  return result;
}

/** Record a read that has just replaced (or, `continues`, extended) the
 * query's data. */
export function recordRead(queryKey: QueryKey, startedAt: number, continues = false): void {
  const hash = hashKey(queryKey);
  const earlier = readAt.get(hash);
  readAt.set(hash, continues && earlier !== undefined ? Math.min(earlier, startedAt) : startedAt);
}

/** When the query's data was read; `fallback` (its dataUpdatedAt) for a
 * query no read was recorded for, such as one restored from storage. */
export function readTimeOf(queryKey: QueryKey, fallback: number): number {
  const recorded = readAt.get(hashKey(queryKey));
  return recorded === undefined ? fallback : Math.min(recorded, fallback);
}
