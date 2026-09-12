/**
 * Per-browser alert preferences for a device with no push subscription --
 * which folders raise a system notification while a page is open. This
 * is deliberately in browser storage, not a server-side setting: there
 * is no per-device row on the server to hang it on until a subscription
 * exists. A device WITH one moves this preference onto that row instead
 * (push_subscriptions.alert_folder_ids, read through
 * use-push.ts's useEffectiveAlertFolderIds) -- this is what the push
 * path degrades to when it is declined or unavailable, not a separate,
 * permanent mechanism.
 *
 * null means "nobody has narrowed this down yet" -- resolved to the
 * folders mail actually arrives in (see isArrivalFolder) rather than to
 * every folder, so a browser or device that has never opened the alerts
 * settings isn't notified about its own Sent, Drafts, Trash and Junk.
 * The same resolution is what useEffectiveAlertFolderIds performs before
 * this ever sees a raw null; an explicit selection -- including one that
 * ticks every folder, outgoing ones included -- is stored and honoured
 * as the concrete list it is.
 */

import { atomWithStorage } from "jotai/utils";

export const alertEnabledFolderIdsAtom = atomWithStorage<string[] | null>(
  "mailverdict:alerts.enabled-folders",
  null,
);

/** Whether a folder is one mail arrives in, as opposed to one it only
 * ever leaves through or lands in as a side effect of something else you
 * did -- Sent, Drafts, Trash, Junk. What an unset alert preference
 * defaults to, so pressing Send doesn't notify you about your own mail. */
/** What a device can mute (push_subscriptions.muted_channels): new mail,
 * and every other alert kind. */
export const PUSH_CHANNELS = ["mail", "system"] as const;
export type PushChannel = (typeof PUSH_CHANNELS)[number];

export function isArrivalFolder(specialUse: string | null | undefined): boolean {
  return specialUse == null || specialUse === "inbox";
}

/** Whether a folder is currently allowed to raise a system notification --
 * the single place this predicate is computed, so the settings checklist
 * and the SSE handler that actually decides whether to call
 * `new Notification(...)` never drift apart. enabledFolderIds is expected
 * pre-resolved (see useEffectiveAlertFolderIds); null here only means "the
 * folder list hasn't loaded yet", not "every folder", so callers with no
 * scope yet fail open rather than notifying for outgoing folders. */
export function folderAlertsEnabled(
  enabledFolderIds: string[] | null,
  folderId: string | null | undefined,
): boolean {
  if (enabledFolderIds === null) return true;
  if (!folderId) return false;
  return enabledFolderIds.includes(folderId);
}
