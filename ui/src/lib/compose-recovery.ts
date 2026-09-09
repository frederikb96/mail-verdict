/**
 * A local, browser-only recovery buffer for a composer's own authored
 * fields -- the crash, reload and closed-tab cases a server-side draft
 * autosave cannot cover, since the outbox insert this app has never
 * returns the resulting message id an autosave would need to update in
 * place (see compose-form.tsx). Attachments are files, not text, and are
 * deliberately not part of this.
 */

export interface ComposeRecoverySnapshot {
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  bodyHtml: string;
}

const STORAGE_PREFIX = "mailverdict:compose-recovery:";

/**
 * Which composer this is, for the recovery buffer's own purposes only --
 * a draft being edited, a reply/forward in progress, or a fresh compose.
 * A fresh compose has nothing else to key on, so every one of them shares
 * one slot; only one is ever open at a time in practice.
 */
export function composeRecoveryKey(
  replacesMessageId: string | undefined, inReplyTo: string | undefined,
): string {
  if (replacesMessageId) return `draft:${replacesMessageId}`;
  if (inReplyTo) return `reply:${inReplyTo}`;
  return "new";
}

function storageKey(composerKey: string): string {
  return `${STORAGE_PREFIX}${composerKey}`;
}

/** `null` covers both "nothing saved" and a corrupt/foreign value under
 * this key -- either way there is nothing usable to offer back. */
export function readComposeRecovery(composerKey: string): ComposeRecoverySnapshot | null {
  try {
    const raw = localStorage.getItem(storageKey(composerKey));
    if (!raw) return null;
    return JSON.parse(raw) as ComposeRecoverySnapshot;
  } catch {
    return null;
  }
}

export function writeComposeRecovery(
  composerKey: string, snapshot: ComposeRecoverySnapshot,
): void {
  try {
    localStorage.setItem(storageKey(composerKey), JSON.stringify(snapshot));
  } catch {
    // Storage full or disabled (private browsing) -- losing the recovery
    // buffer is no worse than never having had one.
  }
}

export function clearComposeRecovery(composerKey: string): void {
  try {
    localStorage.removeItem(storageKey(composerKey));
  } catch {
    // See writeComposeRecovery.
  }
}
