/**
 * Refreshing the rows a mail list already holds, in one request.
 *
 * A mail list is a window of rows loaded from the newest message down to
 * however far the reader has paged. Whenever the folder changes, that whole
 * window is re-read from the newest edge with a single request sized to it,
 * and the fresh rows are spliced over the cached ones here -- so a list
 * scrolled twenty pages deep costs one request per change, not twenty, and
 * no change is ever skipped because the list happens to be deep.
 */

export interface WindowRow {
  id: string;
  received_at: string | null;
}

/** Rows per page, matching what the list asks for when it pages. */
export const WINDOW_PAGE_SIZE = 50;

/** How many rows past the loaded window a refresh reads, so that mail
 * arriving at the top does not push the window's own last rows out of the
 * response. */
const WINDOW_REFRESH_SLACK = 50;

/** Largest window one refresh re-reads -- the list endpoints' own `limit`
 * ceiling (`api/mails.py`, `api/unified.py`). Rows loaded deeper than this
 * are kept as they are rather than re-read. */
const WINDOW_REFRESH_MAX_ROWS = 1000;

export function windowRefreshLimit(loadedRows: number): number {
  return Math.min(Math.max(loadedRows, 0) + WINDOW_REFRESH_SLACK, WINDOW_REFRESH_MAX_ROWS);
}

/**
 * Ids an unread-only window keeps showing after they stop being unread,
 * until the reader navigates away or the toggle turns off and back on --
 * Gmail's own behaviour for its unread filter, and mail-list.tsx's own
 * useEffect clears this on exactly the same identity change that already
 * clears the selection. A plain module-level set rather than React state:
 * nothing here ever needs to trigger a render on its own, only to be
 * consulted the next time a window refresh runs.
 */
export const keptWhileUnreadIds = new Set<string>();

export function markKeptWhileUnread(id: string): void {
  keptWhileUnreadIds.add(id);
}

export function clearKeptWhileUnread(): void {
  keptWhileUnreadIds.clear();
}

/**
 * Whether `a` sits above `b` in a newest-first list -- the server's own
 * `ORDER BY received_at DESC, id DESC`, where PostgreSQL places a NULL
 * received_at first.
 */
function sitsAbove(a: WindowRow, b: WindowRow): boolean {
  if (a.received_at !== b.received_at) {
    if (a.received_at === null) return true;
    if (b.received_at === null) return false;
    const at = Date.parse(a.received_at);
    const bt = Date.parse(b.received_at);
    if (at !== bt) return at > bt;
  }
  return a.id > b.id;
}

/**
 * Splice a freshly read head window over the rows currently loaded.
 *
 * The result covers the same stretch of the folder the reader had loaded
 * and no more: rows the fresh read found below the old last row are left
 * for ordinary paging, so a refresh never grows the list underneath the
 * reader. Rows the fresh read did not reach -- only possible once the
 * window is deeper than a single refresh reads -- are kept as they were.
 *
 * @param current rows currently in the cache, newest first
 * @param fresh rows just read from the newest edge, newest first
 * @param freshHasMore whether the server holds rows past `fresh`
 * @param currentHasMore whether the cached window could page further
 * @param preserveIds rows in `current` that a genuinely fresh read would
 *   have dropped (an unread-only window's own filter, once the reader has
 *   read one of its rows) but that must stay visible regardless -- see
 *   `keptWhileUnreadIds` below. Ignored for a row `fresh` already covers,
 *   which is authoritative.
 */
export function mergeRefreshedWindow<T extends WindowRow>(
  current: T[],
  fresh: T[],
  freshHasMore: boolean,
  currentHasMore: boolean,
  preserveIds?: ReadonlySet<string>,
): { rows: T[]; hasMore: boolean } {
  if (current.length === 0) return { rows: fresh, hasMore: freshHasMore };

  const freshIds = new Set(fresh.map((row) => row.id));
  const preserved = preserveIds
    ? current.filter((row) => preserveIds.has(row.id) && !freshIds.has(row.id))
    : [];
  // Merged back into fresh, in the same newest-first order the server
  // itself would return them in, before any of the windowing below sees
  // them -- otherwise a preserved row sitting above the fresh read's own
  // last row would be treated as "not reached yet" and dropped anyway.
  const effectiveFresh =
    preserved.length === 0
      ? fresh
      : [...fresh, ...preserved].sort((a, b) => (sitsAbove(a, b) ? -1 : sitsAbove(b, a) ? 1 : 0));

  const oldLast = current[current.length - 1];
  const freshLast = effectiveFresh[effectiveFresh.length - 1];

  const reachedOldLast =
    !freshHasMore || (freshLast !== undefined && !sitsAbove(freshLast, oldLast));
  if (reachedOldLast) {
    const rows = effectiveFresh.filter((row) => row.id === oldLast.id || sitsAbove(row, oldLast));
    return { rows, hasMore: freshHasMore || rows.length < effectiveFresh.length };
  }

  // The fresh read stopped above the old last row: everything it covers is
  // authoritative, everything below where it stopped is kept as it was.
  const effectiveFreshIds = new Set(effectiveFresh.map((row) => row.id));
  const kept = current.filter((row) => !effectiveFreshIds.has(row.id) && sitsAbove(freshLast, row));
  return { rows: [...effectiveFresh, ...kept], hasMore: currentHasMore };
}

/** Cut rows into consecutive pages of `WINDOW_PAGE_SIZE` -- at least one
 * page, even for no rows, since an infinite query always holds one. */
export function chunkIntoPages<T>(rows: T[]): T[][] {
  if (rows.length === 0) return [[]];
  const pages: T[][] = [];
  for (let i = 0; i < rows.length; i += WINDOW_PAGE_SIZE) {
    pages.push(rows.slice(i, i + WINDOW_PAGE_SIZE));
  }
  return pages;
}
