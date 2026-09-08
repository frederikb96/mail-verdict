/** Parsing of `mailto:` URLs, for the protocol handler the manifest registers.
 *
 * The registration hands the whole URL back as a query parameter, so what
 * arrives here is whatever the linking page wrote -- including the malformed
 * and the empty. Every failure returns `null` rather than a half-filled
 * compose intent: a composer that opens with the recipient silently missing is
 * worse than one that opens blank.
 */

export interface MailtoIntent {
  to?: string[];
  cc?: string[];
  bcc?: string[];
  subject?: string;
  /** The `body` parameter as HTML, ready for the editor. */
  bodyHtml?: string;
}

/** `decodeURIComponent` throws on a stray `%`, and this runs in an effect at
 * the root of the tree: an unhandled throw there takes the whole application
 * down over a malformed link. An undecodable value is passed through as it
 * came instead. */
function decode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** Addresses arrive comma-separated and percent-encoded, and a trailing or
 * doubled comma is common enough in real links to be worth surviving. */
function addresses(value: string | null): string[] | undefined {
  if (!value) return undefined;
  const list = value
    .split(",")
    .map((entry) => decode(entry.trim()))
    .filter(Boolean);
  return list.length > 0 ? list : undefined;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** Plain text to the paragraph markup the editor expects. A blank line
 * separates paragraphs; a single newline is a line break inside one. */
function textToHtml(value: string): string {
  return value
    .split(/\n{2,}/)
    .map((block) => `<p>${escapeHtml(block).replace(/\n/g, "<br>")}</p>`)
    .join("");
}

export function parseMailto(raw: string): MailtoIntent | null {
  const trimmed = raw.trim();
  if (!trimmed.toLowerCase().startsWith("mailto:")) return null;

  const rest = trimmed.slice("mailto:".length);
  const split = rest.indexOf("?");
  const path = split === -1 ? rest : rest.slice(0, split);
  const query = new URLSearchParams(split === -1 ? "" : rest.slice(split + 1));

  // A parameter may repeat and may also duplicate the path recipients; the
  // union is what the sender meant, so recipients are merged and de-duplicated
  // rather than one source winning.
  const to = [...(addresses(path) ?? []), ...(addresses(query.get("to")) ?? [])];
  const body = query.get("body");
  const subject = query.get("subject") ?? undefined;

  const intent: MailtoIntent = {
    to: to.length > 0 ? [...new Set(to)] : undefined,
    cc: addresses(query.get("cc")),
    bcc: addresses(query.get("bcc")),
    subject: subject || undefined,
    bodyHtml: body ? textToHtml(body) : undefined,
  };

  const carries = Object.values(intent).some((field) => field !== undefined);
  return carries ? intent : null;
}
