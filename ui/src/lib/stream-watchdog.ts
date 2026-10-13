/**
 * Telling a live stream that went quiet from one that is merely idle.
 *
 * EventSource hides comment lines, so the server's heartbeat is a named
 * `ping` event. After a laptop sleeps or the network changes, the socket can
 * stay open on this side while nothing reaches it any more; the browser may
 * not notice for minutes. A stream that has pinged at least once and then
 * stays silent for STALE_AFTER_MS is dropped and opened again.
 */

/** Silence after which a stream that has pinged is considered dead; the
 * server pings every 15 seconds. */
export const STALE_AFTER_MS = 40_000;

/** How often the silence is checked while the page is running. */
export const WATCHDOG_INTERVAL_MS = 10_000;

export interface StreamActivity {
  /** When the last thing, a heartbeat or an event, arrived. */
  lastAt: number;
  /** Whether this connection has delivered a heartbeat: a server that does
   * not send one is never judged by its silence. */
  pinged: boolean;
}

export function streamIsStale(activity: StreamActivity, now: number): boolean {
  return activity.pinged && now - activity.lastAt > STALE_AFTER_MS;
}
