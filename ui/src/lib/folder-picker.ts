/**
 * The decisions behind the folder picker, apart from its rendering: which
 * folders count as system folders, how a filter matches, what an account's
 * tri-state box shows and what a click on it does.
 */

import { folderDisplayName } from "@/lib/folders";
import type { FolderResponse } from "@/types/api";

/** Folders mail leaves through or lands in as a side effect, plus the ones
 * the sidebar hides (an Exchange account's Calendar, Contacts, Journal). */
const SYSTEM_USES = new Set(["drafts", "sent", "trash", "junk"]);

export function isSystemFolder(
  folder: Pick<FolderResponse, "special_use" | "is_visible">,
): boolean {
  return !folder.is_visible || (folder.special_use !== null && SYSTEM_USES.has(folder.special_use));
}

const SPECIAL_USE_ORDER = ["inbox", "drafts", "sent", "archive", "junk", "trash"];

/** Inbox and the other special-use folders first in their usual order, then
 * the rest by name. */
export function sortPickerFolders(folders: FolderResponse[]): FolderResponse[] {
  const rank = (f: FolderResponse) => {
    const i = f.special_use ? SPECIAL_USE_ORDER.indexOf(f.special_use) : -1;
    return i === -1 ? SPECIAL_USE_ORDER.length : i;
  };
  return [...folders].sort(
    (a, b) => rank(a) - rank(b) || folderDisplayName(a).localeCompare(folderDisplayName(b)),
  );
}

/** The folders of one account the picker lists: system folders hidden on
 * request -- except ones already ticked, which would otherwise count
 * toward a total nobody can see -- and, with a query, only the matches.
 * A query that names the account matches all of its folders. */
export function listedFolders(
  folders: FolderResponse[],
  accountName: string,
  options: { hideSystem: boolean; query: string; selected: ReadonlySet<string> },
): FolderResponse[] {
  const query = options.query.trim().toLowerCase();
  const accountMatches = query !== "" && accountName.toLowerCase().includes(query);
  return sortPickerFolders(folders).filter((folder) => {
    if (options.hideSystem && isSystemFolder(folder) && !options.selected.has(folder.id)) {
      return false;
    }
    if (query === "" || accountMatches) return true;
    return folderDisplayName(folder).toLowerCase().includes(query);
  });
}

export type GroupState = "all" | "some" | "none";

export function groupState(listedIds: string[], selected: ReadonlySet<string>): GroupState {
  const ticked = listedIds.filter((id) => selected.has(id)).length;
  if (ticked === 0) return "none";
  return ticked === listedIds.length ? "all" : "some";
}

/** Ticking an account's box ticks everything it lists; clicking it while
 * everything is ticked clears it. Order of `value` is kept, additions
 * follow it. */
export function toggleGroup(value: string[], listedIds: string[]): string[] {
  const selected = new Set(value);
  if (groupState(listedIds, selected) === "all") {
    const drop = new Set(listedIds);
    return value.filter((id) => !drop.has(id));
  }
  return [...value, ...listedIds.filter((id) => !selected.has(id))];
}

export function toggleFolder(value: string[], folderId: string, checked: boolean): string[] {
  const has = value.includes(folderId);
  if (checked) return has ? value : [...value, folderId];
  return has ? value.filter((id) => id !== folderId) : value;
}
