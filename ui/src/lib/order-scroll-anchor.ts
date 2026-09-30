/**
 * Remembers exactly where a reader was -- in the list and inside the
 * order's own detail -- the instant before they open a mail out of an
 * order, so browser Back can put both panes back where they sat on screen
 * rather than at the top of either.
 *
 * sessionStorage rather than component state: `useOpenMessage` navigates
 * away from /orders entirely (to the mail view), which unmounts this
 * screen -- there is nowhere in memory left to hold it.
 */

const STORAGE_KEY = "mv.orders.anchor";

export interface OrderScrollAnchor {
  orderId: string;
  mailKey: string;
  /** The mail row's distance from the top of the detail pane's own
   * viewport at the moment it was opened. */
  rowTop: number;
  /** The order's index in the list as it was shown at that moment. */
  listIndex: number;
  /** The order row's distance from the top of the list's own viewport. */
  listRowTop: number;
}

export function writeOrderScrollAnchor(anchor: OrderScrollAnchor): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(anchor));
  } catch {
    // Private browsing or a full quota -- losing the anchor only costs the
    // restore, never correctness.
  }
}

/** The anchor, only when it names this exact order -- a stale one from a
 * different order (the reader went elsewhere without ever coming back) is
 * not this order's to restore. */
export function readOrderScrollAnchor(orderId: string): OrderScrollAnchor | null {
  let raw: string | null;
  try {
    raw = sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as OrderScrollAnchor;
    return parsed.orderId === orderId ? parsed : null;
  } catch {
    return null;
  }
}

export function clearOrderScrollAnchor(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Same as above.
  }
}
