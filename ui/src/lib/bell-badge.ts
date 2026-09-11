/** The alert kind that is new mail. Every other kind -- a send stuck on its
 * way out, and any kind added later -- is a system notification: listed
 * under System, and always in the badge. */
export const MAIL_ALERT_KIND = "mail";

export function isMailAlertKind(kind: string): boolean {
  return kind === MAIL_ALERT_KIND;
}

/** What the notification bell's badge counts -- decided here and nowhere
 * else. System notifications always count: a write that never reached the
 * mail server, and every alert that is not new mail. New-mail alerts count
 * only while settings.mail.bell_badge_counts_new_mail is on; until that
 * setting has loaded they are left out rather than guessed at. The bell's
 * own lists show everything either way. */
export function bellBadgeCount({
  unseenAlertsByKind,
  unacknowledgedNotifications,
  countsNewMail,
}: {
  unseenAlertsByKind: Record<string, number>;
  unacknowledgedNotifications: number;
  countsNewMail: boolean | undefined;
}): number {
  let count = unacknowledgedNotifications;
  for (const [kind, unseen] of Object.entries(unseenAlertsByKind)) {
    if (countsNewMail === true || !isMailAlertKind(kind)) count += unseen;
  }
  return count;
}
