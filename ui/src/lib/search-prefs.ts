/**
 * Search page view preferences -- per-device, not account state, so
 * localStorage like the rest of this app's view preferences
 * (threadedViewAtom, calendarViewAtom in lib/atoms.ts). Kept in their own
 * file rather than added to the shared atoms module, since the search
 * page is the only thing that reads or writes these.
 */

import { atomWithStorage } from "jotai/utils";
import type { CacheSnapshot } from "virtua";
import type { SearchField, SearchStrictness } from "@/types/api";

export const ALL_SEARCH_FIELDS: SearchField[] = ["subject", "from", "to", "body"];

/** Which fields a fulltext search scans. Semantic mode ignores this --
 * there is one embedding per message, nothing to scope by field. */
export const searchFieldsAtom = atomWithStorage<SearchField[]>(
  "mailverdict:search-fields",
  ALL_SEARCH_FIELDS,
);

/** null means "every folder" -- including any created after this was last
 * saved. An explicit id list only appears once the reader has actually
 * deselected something; hitting "select all" clears it back to null
 * rather than writing out every id, so a folder added later is still
 * included by default instead of silently excluded. */
export const searchFolderIdsAtom = atomWithStorage<string[] | null>(
  "mailverdict:search-folders",
  null,
);

/** Fulltext (fuzzy, field-scoped) vs semantic (meaning, no field scope). */
export const searchSemanticModeAtom = atomWithStorage<boolean>(
  "mailverdict:search-semantic",
  false,
);

/** The typed query text -- persisted alongside scope and mode so opening a
 * result and pressing Back returns to the same search rather than an
 * empty box; the other two already survived a Back, this one did not. */
export const searchQueryAtom = atomWithStorage<string>("mailverdict:search-query", "");

/** How tightly semantic results cluster around the best match -- a
 * position name, not a float: the underlying cutoff is relative to the
 * best match in the pool, so a raw number would read as an absolute
 * threshold it isn't (see the backend's embeddings/search.py). */
export const searchStrictnessAtom = atomWithStorage<SearchStrictness>(
  "mailverdict:search-strictness",
  "balanced",
);

/** The result row at the top of the viewport, captured on scroll and
 * restored on mount -- listIdentity ties it to the exact search it was
 * captured under (query, mode, fields, folders, strictness -- the same
 * key search-page.tsx keys its VList on), so a return to a *different*
 * search can never apply a stale anchor left over from another one. */
export interface SearchScrollAnchor {
  listIdentity: string;
  messageId: string;
}
export const searchScrollAnchorAtom = atomWithStorage<SearchScrollAnchor | null>(
  "mailverdict:search-scroll-anchor",
  null,
);

/** virtua's own measured-row-height cache, keyed to the same listIdentity
 * as the anchor above. Restoring it alongside the anchor is what keeps a
 * restored row from re-measuring and shoving the anchor out from under
 * the reader (SKILL.md's restore-and-hold) -- the two are complementary,
 * cache for heights and anchor for position. */
export interface SearchScrollCache {
  listIdentity: string;
  cache: CacheSnapshot;
}
export const searchScrollCacheAtom = atomWithStorage<SearchScrollCache | null>(
  "mailverdict:search-scroll-cache",
  null,
);
