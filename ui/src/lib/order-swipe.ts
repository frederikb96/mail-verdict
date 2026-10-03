/**
 * The decisions behind an order row's touch swipe, kept apart from the
 * pointer plumbing so they can be unit tested -- see order-swipe.test.ts.
 *
 * Right-to-left (negative dx) toggles favorite, left-to-right (positive
 * dx) toggles closed/open.
 */

import type { OrderActionId } from "@/lib/order-actions";

/** Movement before the gesture is read as horizontal or vertical. */
export const SWIPE_LOCK_PX = 10;
/** Horizontal travel at which releasing commits the action. */
export const SWIPE_COMMIT_PX = 80;
/** How far the row follows the finger at most. */
export const SWIPE_MAX_PX = 120;

export type SwipeAxis = "pending" | "horizontal" | "vertical";

/** Which way a gesture is going once it has moved far enough to tell.
 * Anything not clearly horizontal is left to the browser's vertical
 * scroll. */
export function swipeAxis(dx: number, dy: number): SwipeAxis {
  const ax = Math.abs(dx);
  const ay = Math.abs(dy);
  if (Math.max(ax, ay) < SWIPE_LOCK_PX) return "pending";
  return ax > ay ? "horizontal" : "vertical";
}

/** The action a swipe of `dx` commits on release, or null when it fell
 * short of the threshold. */
export function swipeAction(dx: number): Extract<OrderActionId, "favorite" | "close"> | null {
  if (dx <= -SWIPE_COMMIT_PX) return "favorite";
  if (dx >= SWIPE_COMMIT_PX) return "close";
  return null;
}

/** Where the row sits for a finger `dx` from where it landed: one-to-one
 * up to the commit point, then increasingly resisting up to the cap, so
 * passing the threshold is felt rather than the row running away. */
export function swipeOffset(dx: number): number {
  const sign = dx < 0 ? -1 : 1;
  const ax = Math.abs(dx);
  if (ax <= SWIPE_COMMIT_PX) return dx;
  const extra = ax - SWIPE_COMMIT_PX;
  const room = SWIPE_MAX_PX - SWIPE_COMMIT_PX;
  return sign * (SWIPE_COMMIT_PX + room * (1 - Math.exp(-extra / room)));
}
