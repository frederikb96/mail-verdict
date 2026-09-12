/** The alert kind that is new mail. Every other kind -- a send stuck on its
 * way out, and any kind added later -- is a system notification: listed
 * under System. What the badge counts is decided by the server
 * (GET /api/alerts/badge), not here. */
export const MAIL_ALERT_KIND = "mail";

export function isMailAlertKind(kind: string): boolean {
  return kind === MAIL_ALERT_KIND;
}
