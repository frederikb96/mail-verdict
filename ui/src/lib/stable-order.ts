/**
 * Reconciling a freshly fetched list against what is already on screen,
 * without moving a row a reader is looking at.
 *
 * At the top of the list a refetch is shown at once -- there is nothing to
 * protect. Scrolled down, an existing row's content refreshes in place but
 * its position never changes, and anything new to the list (a fresh order,
 * or one an update bumped past the visible window) is held back rather
 * than inserted above the reader; the caller shows those as a single "New
 * activity" affordance and takes them over on demand.
 *
 * A pure function so it can be unit tested without React or a browser --
 * see stable-order.test.ts.
 */

export interface StableOrderResult<T> {
  /** What to render, in the exact order it was already on screen (minus
   * anything gone from `fresh`), each entry refreshed with `fresh`'s own
   * copy of it. */
  rows: T[];
  /** Present in `fresh` but not among `shown` -- new to the list, or moved
   * far enough up that they left the reader's stable window. Never
   * rendered directly; the caller offers them through its own affordance. */
  held: T[];
}

export function stableOrder<T extends { id: string }>(
  shown: T[],
  fresh: T[],
  atTop: boolean,
): StableOrderResult<T> {
  if (atTop) {
    return { rows: fresh, held: [] };
  }

  const freshById = new Map(fresh.map((item) => [item.id, item]));
  const shownIds = new Set(shown.map((item) => item.id));

  const rows: T[] = [];
  for (const item of shown) {
    const current = freshById.get(item.id);
    if (current) rows.push(current);
  }

  const held = fresh.filter((item) => !shownIds.has(item.id));

  return { rows, held };
}
