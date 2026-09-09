/**
 * Per-browser alert preferences -- which folders raise a system
 * notification. This is deliberately in browser storage, not a
 * server-side setting: there is no push subscription yet (see the alert
 * design), so there is no per-device row on the server to hang this on.
 * Once push exists, a device WITH a subscription moves this preference
 * onto that row instead (push_subscriptions.alert_folder_ids) -- this is
 * what the push path degrades to when it is declined or unavailable, not
 * a separate, permanent mechanism.
 *
 * null means "every folder" -- the same NULL-means-nobody-narrowed-it-
 * down convention push_subscriptions.alert_folder_ids itself documents,
 * so a browser that has never opened the alerts settings still gets
 * notified for everything rather than silently nothing.
 */

import { atomWithStorage } from "jotai/utils";

export const alertEnabledFolderIdsAtom = atomWithStorage<string[] | null>(
  "mailverdict:alerts.enabled-folders",
  null,
);

/** Whether a folder is currently allowed to raise a system notification --
 * the single place this predicate is computed, so the settings checklist
 * and the SSE handler that actually decides whether to call
 * `new Notification(...)` never drift apart. */
export function folderAlertsEnabled(
  enabledFolderIds: string[] | null,
  folderId: string | null | undefined,
): boolean {
  if (enabledFolderIds === null) return true;
  if (!folderId) return false;
  return enabledFolderIds.includes(folderId);
}
