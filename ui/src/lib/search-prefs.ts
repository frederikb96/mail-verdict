/**
 * Search page view preferences -- per-device, not account state, so
 * localStorage like the rest of this app's view preferences
 * (threadedViewAtom, calendarViewAtom in lib/atoms.ts). Kept in their own
 * file rather than added to the shared atoms module, since the search
 * page is the only thing that reads or writes these.
 */

import { atomWithStorage } from "jotai/utils";
import type { SearchField } from "@/types/api";

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
