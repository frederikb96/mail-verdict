/**
 * Recognising a glacier folder client-side. There is one per account
 * (account_prefs.glacier_folder_id, once enabled), used everywhere a
 * folder id is used -- so a move whose target happens to be one needs its
 * own confirmation and must never be offered undo, both decided here
 * rather than re-derived at each call site.
 */

import type { AccountResponse } from "@/types/api";

/** Every enabled glacier's synthetic folder id, across every account. */
export function glacierFolderIds(accounts: AccountResponse[] | undefined): Set<string> {
  const ids = new Set<string>();
  for (const account of accounts ?? []) {
    if (account.glacier_enabled && account.glacier_folder_id) {
      ids.add(account.glacier_folder_id);
    }
  }
  return ids;
}

export function isGlacierFolder(
  folderId: string | undefined | null,
  glacierIds: Set<string>,
): boolean {
  return !!folderId && glacierIds.has(folderId);
}

/** What every glacier-move confirmation says, whatever surface it is
 * shown from -- one wording rather than four that drift. */
export function glacierMoveWarning(count: number): string {
  return count === 1
    ? "This message leaves the mail server for good and will only be found here afterward. This cannot be undone."
    : `These ${count} messages leave the mail server for good and will only be found here afterward. This cannot be undone.`;
}

/** The other direction: Archive, Move to trash and an explicit Move all
 * restore a glaciered message to the mail server -- never fired without
 * this confirmation, the same way entering the glacier needs one. One
 * wording, reused wherever a restore can be started. */
export function glacierRestoreWarning(count: number): string {
  return count === 1
    ? "This message goes back onto the mail server and counts toward your mailbox's storage again."
    : `These ${count} messages go back onto the mail server and count toward your mailbox's storage again.`;
}
