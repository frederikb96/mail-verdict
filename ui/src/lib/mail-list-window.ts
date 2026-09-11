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
 */
export function mergeRefreshedWindow<T extends WindowRow>(
  current: T[],
  fresh: T[],
  freshHasMore: boolean,
  currentHasMore: boolean,
): { rows: T[]; hasMore: boolean } {
  if (current.length === 0) return { rows: fresh, hasMore: freshHasMore };
  const oldLast = current[current.length - 1];
  const freshLast = fresh[fresh.length - 1];

  const reachedOldLast =
    !freshHasMore || (freshLast !== undefined && !sitsAbove(freshLast, oldLast));
  if (reachedOldLast) {
    const rows = fresh.filter((row) => row.id === oldLast.id || sitsAbove(row, oldLast));
    return { rows, hasMore: freshHasMore || rows.length < fresh.length };
  }

  // The fresh read stopped above the old last row: everything it covers is
  // authoritative, everything below where it stopped is kept as it was.
  const freshIds = new Set(fresh.map((row) => row.id));
  const kept = current.filter((row) => !freshIds.has(row.id) && sitsAbove(freshLast, row));
  return { rows: [...fresh, ...kept], hasMore: currentHasMore };
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
