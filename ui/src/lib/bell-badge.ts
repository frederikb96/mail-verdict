/** What the notification bell's badge counts -- decided here and nowhere
 * else. System notifications (a write that never reached the mail server)
 * always count; new-mail alerts count only while
 * settings.mail.bell_badge_counts_new_mail is on. Until that setting has
 * loaded, mail alerts are left out rather than guessed at. The bell's own
 * lists show everything either way. */
export function bellBadgeCount({
  unseenMailAlerts,
  unacknowledgedSystem,
  countsNewMail,
}: {
  unseenMailAlerts: number;
  unacknowledgedSystem: number;
  countsNewMail: boolean | undefined;
}): number {
  return unacknowledgedSystem + (countsNewMail === true ? unseenMailAlerts : 0);
}
