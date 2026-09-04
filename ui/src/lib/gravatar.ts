/** Gravatar avatar URLs, keyed by a SHA-256 hash of the address.
 *
 * The Web Crypto API already ships in every browser this app targets, so
 * this needs no hashing dependency of its own.
 */

async function sha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await window.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * The Gravatar image URL for an email address, or null once we already
 * know there is nothing to fetch.
 *
 * `d=404` asks Gravatar to answer with a 404 rather than its own
 * placeholder image when the address has none -- the caller's own
 * initials fallback is what should show instead, not a generic icon
 * that misleadingly suggests a photo was found.
 */
export async function gravatarUrl(email: string, size: number): Promise<string | null> {
  const normalized = email.trim().toLowerCase();
  if (!normalized) return null;
  const hash = await sha256Hex(normalized);
  return `https://www.gravatar.com/avatar/${hash}?s=${size}&d=404`;
}
