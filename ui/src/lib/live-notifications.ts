/**
 * Raising a system notification for a mail alert, and reconciling the
 * ones already shown against which alerts are still live -- the latter
 * used by use-sse.ts's alert.dismissed handler to close a notification
 * whose alert just resolved (read elsewhere, archived, dismissed),
 * rather than leaving it sitting there until the reader dismisses it by
 * hand.
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

/** The icon a push notification is raised with in public/sw.js, repeated
 * here so an alert looks the same whichever route raised it. */
const ALERT_ICON = "/icon-192.png";

/** What an alert needs to become a system notification. The id is the
 * tag, and the only handle there is for closing it again later. */
export interface AlertNotification {
  id?: string;
  title: string;
  body?: string;
  url?: string;
}

/**
 * Raises a system notification for a new alert, through the service
 * worker wherever this browser has one registered.
 *
 * Which route raises it is not cosmetic. A push raises its notification
 * from the service worker, and a browser holding a push subscription
 * necessarily has a registration -- so on such a browser the same alert
 * is raised twice, once from the SSE event that brings it here and once
 * from the push a moment later. Sharing a tag is what collapses those
 * two into one, and a tag only does that between notifications of the
 * same kind: one constructed as `new Notification` and one shown through
 * a registration are different kinds, and both stay on screen. Going
 * through the registration whenever there is one puts both on the same
 * side of that line.
 *
 * A browser without a registration receives no push either (see
 * use-push.ts, which registers only while subscribing), so raising it
 * directly there cannot duplicate anything.
 */
export async function showAlertNotification(alert: AlertNotification): Promise<void> {
  const options: NotificationOptions = {
    body: alert.body,
    tag: alert.id,
    icon: ALERT_ICON,
    data: { url: alert.url || "/" },
  };

  try {
    const registration =
      typeof navigator !== "undefined" && "serviceWorker" in navigator
        ? await navigator.serviceWorker.getRegistration("/")
        : undefined;

    if (registration) {
      // Clicking it is handled by the service worker's notificationclick,
      // the same path a push notification's click already takes.
      await registration.showNotification(alert.title, options);
      return;
    }

    const notification = new Notification(alert.title, options);
    if (alert.id) trackPageNotification(alert.id, notification);
    notification.onclick = () => {
      window.focus();
      if (alert.url) window.location.href = alert.url;
      notification.close();
    };
  } catch {
    // Best-effort, and awaited by nobody: a browser that refuses to
    // raise it still has the bell, which the caller refreshed first.
  }
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
