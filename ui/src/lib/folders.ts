/** How a folder's own name is decided for display -- one definition,
 * shared everywhere a folder name is rendered rather than each surface
 * reaching for `imap_name` on its own. */

import type { FolderOrderItem, FolderResponse } from "@/types/api";

type NamedFolder = Pick<FolderResponse | FolderOrderItem, "imap_name" | "display_name" | "special_use">;

/** A special-use folder's own display name is a mirror write (PostIMAP's
 * `display_name`) or a user override (folder_prefs), and both are often
 * simply absent -- an IMAP server has no obligation to advertise one, and
 * a user rarely bothers renaming a folder that already reads as "Inbox"
 * in their own client. Falling back to the raw server name then means
 * Posteo's "INBOX" or an Exchange account's "Gelöschte Elemente" leaking
 * straight into the sidebar. This is the one place special-use folders
 * fall back to a role name instead. */
const SPECIAL_USE_LABELS: Record<string, string> = {
  inbox: "Inbox",
  drafts: "Drafts",
  sent: "Sent",
  archive: "Archive",
  junk: "Junk",
  trash: "Trash",
};

/** The name a folder renders with -- an explicit display name first, then
 * a role label for a recognised special-use folder, then the raw server
 * name. A custom folder (no special_use) always falls through to its own
 * name, which is exactly what someone gave it. */
export function folderDisplayName(folder: NamedFolder): string {
  if (folder.display_name) return folder.display_name;
  if (folder.special_use && SPECIAL_USE_LABELS[folder.special_use]) {
    return SPECIAL_USE_LABELS[folder.special_use];
  }
  return folder.imap_name;
}
