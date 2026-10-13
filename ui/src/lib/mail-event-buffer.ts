/**
 * What a burst of mail.new / mail.updated / mail.deleted events asks the
 * client to re-read, and which of those events are only the echo of this
 * browser's own mail action.
 *
 * An own action re-reads everything it touched once the server answers it
 * (use-mail-intents.ts), so its echo over the event stream would trigger the
 * same re-read a second time. The echo is recognised by message id and
 * timing alone: the event arrived after the request left and no later than a
 * short grace after the answer. Events for a message whose request is still
 * out are held until it is answered. Other clients' changes never match,
 * because only ids this browser has an intent for are looked at.
 */

import type { MailIntent } from "@/lib/mail-intents";
import type { SSEEvent } from "@/types/api";

/** How long after an intent was answered an event for one of its messages
 * still counts as its echo: the event and the response race each other. */
export const ECHO_GRACE_MS = 1500;

/** How long an event is held for a request that has not been answered. */
export const ECHO_HOLD_MAX_MS = 5000;

export type BufferedMailEvent = {
  kind: "new" | "updated" | "deleted";
  /** The message, when the payload named one. */
  id: string | null;
  /** Folders the event names; none means every open list is re-read. */
  folderIds: string[];
  /** A mail.updated that changed the folder. */
  moved: boolean;
  /** The payload could not be read. */
  unparsed: boolean;
  arrivedAt: number;
};

/** The buffered form of one event; `data` is null for an unreadable payload. */
export function bufferMailEvent(
  kind: BufferedMailEvent["kind"], data: SSEEvent | null, arrivedAt: number,
): BufferedMailEvent {
  if (!data) return { kind, id: null, folderIds: [], moved: false, unparsed: true, arrivedAt };
  const folderIds = [data.folder_id, kind === "updated" ? data.old_folder_id : undefined].filter(
    (id): id is string => !!id,
  );
  return {
    kind, id: data.id ?? null, folderIds, arrivedAt, unparsed: false,
    moved: kind === "updated" && !!data.id && !!data.changed?.includes("folder_id"),
  };
}

export type Echo = "none" | "settled" | "inflight";

function touches(intent: MailIntent, id: string): boolean {
  return (
    intent.messages.some((m) => m.id === id) ||
    !!intent.skippedIds?.includes(id) ||
    !!intent.sources?.some((s) => s.id === id)
  );
}

/**
 * Whether an event for `id` that arrived at `arrivedAt` is the echo of an
 * intent: "settled" when that intent's answer, and so its re-read, covers
 * it, "inflight" when the request is still out.
 */
export function ownEcho(id: string, arrivedAt: number, intents: readonly MailIntent[]): Echo {
  let echo: Echo = "none";
  for (const intent of intents) {
    if (intent.firstSentAt === undefined || arrivedAt < intent.firstSentAt) continue;
    if (!touches(intent, id)) continue;
    if (intent.state === "done" && arrivedAt <= (intent.doneAt ?? 0) + ECHO_GRACE_MS) {
      return "settled";
    }
    if (intent.state === "inflight") echo = "inflight";
  }
  return echo;
}

/** Splits a burst into events to act on and events to hold for another look. */
export function triageMailEvents(
  events: readonly BufferedMailEvent[], intents: readonly MailIntent[], now: number,
): { apply: BufferedMailEvent[]; hold: BufferedMailEvent[] } {
  const apply: BufferedMailEvent[] = [];
  const hold: BufferedMailEvent[] = [];
  for (const event of events) {
    const echo = event.id ? ownEcho(event.id, event.arrivedAt, intents) : "none";
    if (echo === "settled") continue;
    if (echo === "inflight" && now - event.arrivedAt < ECHO_HOLD_MAX_MS) hold.push(event);
    else apply.push(event);
  }
  return { apply, hold };
}

export interface MailRefreshPlan {
  /** Messages whose own record changed. */
  updatedIds: Set<string>;
  /** Messages that moved away or were deleted. */
  removedIds: Set<string>;
  /** Mail arrived or moved in, or a delete named no message. */
  newOrMoved: boolean;
  folderCounts: boolean;
  /** The folders the events touch; null when one named none. */
  folders: Set<string> | null;
}

/** What re-reading for these events means. */
export function planMailRefresh(events: readonly BufferedMailEvent[]): MailRefreshPlan {
  const plan: MailRefreshPlan = {
    updatedIds: new Set(), removedIds: new Set(), newOrMoved: false, folderCounts: false,
    folders: new Set(),
  };
  for (const event of events) {
    if (event.folderIds.length === 0) plan.folders = null;
    else for (const id of event.folderIds) plan.folders?.add(id);
    if (event.unparsed) {
      plan.newOrMoved = true;
      continue;
    }
    if (event.kind === "new") {
      plan.newOrMoved = true;
      if (event.folderIds.length > 0) plan.folderCounts = true;
    } else if (event.kind === "updated") {
      if (event.moved && event.id) {
        plan.removedIds.add(event.id);
        plan.newOrMoved = true;
      } else if (event.id) {
        plan.updatedIds.add(event.id);
      }
      plan.folderCounts = true;
    } else {
      if (event.id) plan.removedIds.add(event.id);
      else plan.newOrMoved = true;
      plan.folderCounts = true;
    }
  }
  return plan;
}
