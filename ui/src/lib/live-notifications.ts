/**
 * Reconciling system notifications already shown against which mail
 * alerts are still live -- used by use-sse.ts's alert.dismissed handler
 * to close a notification whose alert just resolved (read elsewhere,
 * archived, dismissed), rather than leaving it sitting there until the
 * reader dismisses it by hand.
 *
 * There is no id on alert.dismissed itself to close a specific
 * notification by (it is a broadcast telling every open page to refresh,
 * see alerts/resolve.py) -- so this reads the *current* undismissed set
 * instead and closes whatever no longer belongs to it. That is also more
 * robust than trusting a per-event id: it self-heals regardless of which
 * event triggered the reconciliation, or how many alerts changed between
 * one and the next.
 */

/** Pure and unit-tested directly; the Notification/ServiceWorkerRegistration
 * calls around it are not available outside a real browser. */
export function idsToClose(shownTags: readonly string[], liveIds: ReadonlySet<string>): string[] {
  return [...new Set(shownTags)].filter((tag) => !liveIds.has(tag));
}

/** In-page Notification() instances created directly (not through the
 * service worker's showNotification, which push uses) -- keyed by tag,
 * the only way to close one later since there is no Notification.get(). */
const pageNotifications = new Map<string, Notification>();

export function trackPageNotification(tag: string, notification: Notification): void {
  pageNotifications.set(tag, notification);
  notification.addEventListener("close", () => pageNotifications.delete(tag));
}

/** Closes every currently shown notification -- raised directly by this
 * page, or by the service worker from a push -- whose tag names an alert
 * that is no longer live. Best-effort: a missing Notification API or no
 * active service worker registration just means there is nothing to
 * close via that route. */
export async function closeResolvedNotifications(liveIds: ReadonlySet<string>): Promise<void> {
  for (const tag of idsToClose([...pageNotifications.keys()], liveIds)) {
    pageNotifications.get(tag)?.close();
    pageNotifications.delete(tag);
  }

  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
  try {
    const registration = await navigator.serviceWorker.ready;
    const shown = await registration.getNotifications();
    const shownTags = shown.map((n) => n.tag).filter((tag): tag is string => !!tag);
    const toClose = new Set(idsToClose(shownTags, liveIds));
    for (const n of shown) {
      if (n.tag && toClose.has(n.tag)) n.close();
    }
  } catch {
    // No active registration, or the browser refused -- nothing else to do.
  }
}
