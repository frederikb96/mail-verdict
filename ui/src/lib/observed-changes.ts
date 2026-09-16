/**
 * Moves and removals other clients made, as the event stream reports them,
 * shown on every list at once rather than when each list is next re-read.
 *
 * Each is projected as an already-answered intent (mail-intents.ts) dated
 * when the event arrived, so it hides the row only from data read before
 * that and never touches a count -- the counts are re-read for the same
 * event. Kept in memory for a short while: the scoped refresh the same
 * event triggers replaces every list it concerns long before.
 */

import type { MailIntent } from "@/lib/mail-intents";

const KEEP_MS = 2 * 60_000;

let changes: MailIntent[] = [];
const listeners = new Set<() => void>();

/** A message another client moved to `toFolderId`, or removed (null). Only
 * the latest change to a message is kept: events replayed on reconnect
 * arrive in order, and all of them dated now. */
export function recordObservedChange(
  messageId: string, fromFolderId: string | null, toFolderId: string | null,
): void {
  const now = Date.now();
  changes = [
    ...changes.filter((c) => now - (c.doneAt ?? 0) < KEEP_MS && c.messages[0]?.id !== messageId),
    {
      id: `observed:${messageId}:${now}`, accountId: "", bulk: false, generation: 0,
      action: toFolderId ? "move" : "expunge", targetFolderId: toFolderId ?? undefined,
      messages: [{ id: messageId, folderId: fromFolderId, isSeen: true, isFlagged: false, threadId: null }],
      createdAt: now, updatedAt: now, notBefore: now, attempts: 1, state: "done", doneAt: now,
      firstSentAt: 0,
    },
  ];
  for (const listener of listeners) listener();
}

export function subscribeObservedChanges(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getObservedChanges(): readonly MailIntent[] {
  return changes;
}

const NONE: readonly MailIntent[] = [];
export function getServerObservedChanges(): readonly MailIntent[] {
  return NONE;
}
