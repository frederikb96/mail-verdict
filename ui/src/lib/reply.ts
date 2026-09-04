/** Recipient, subject and threading-header derivation for reply/reply-all
 * and forward, plus the plain-text and attribution halves of a quote. */

import { extractEmail, extractSenderName, formatFullDate } from "@/lib/format";
import type { MessageDetail } from "@/types/api";

export interface ReplyDraft {
  to: string[];
  cc: string[];
  subject: string;
  /** The `> `-prefixed plain-text quote, appended below the authored
   * markdown to build body_text -- see compose-form.tsx. */
  quotedText: string;
  /** "On <date>, <name> wrote:" -- the attribution line shown above the
   * quote embedded in the editor, and the source quotedText's own first
   * line, so the two forms of the quote agree with each other. */
  attribution: string;
  inReplyTo?: string;
  references?: string[];
}

export interface ForwardDraft {
  subject: string;
  quotedText: string;
  attribution: string;
}

function dedupeExcluding(addrs: string[], exclude: Set<string>): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const addr of addrs) {
    const key = addr.toLowerCase();
    if (!addr || seen.has(key) || exclude.has(key)) continue;
    seen.add(key);
    result.push(addr);
  }
  return result;
}

function subjectWithPrefix(subject: string | null): string {
  const base = subject ?? "(no subject)";
  return /^re:/i.test(base) ? base : `Re: ${base}`;
}

function replyAttribution(source: MessageDetail): string {
  const sender = extractSenderName(source.from_addr);
  const date = formatFullDate(source.received_at);
  return `On ${date}, ${sender} wrote:`;
}

/** The `> `-prefixed plain-text form of a quote, built from the original
 * message's own body_text -- unrelated to the HTML quote embedded in the
 * editor, which comes from GET /api/messages/:id/quote instead. Kept as
 * plain text because that is the conventional readable rendering a plain
 * mail reader expects, and because it is appended below the editor's own
 * markdown export rather than parsed back into anything. */
function quotedPlainText(source: MessageDetail, attribution: string): string {
  const body = source.body_text ?? "";
  const quoted = body
    .split("\n")
    .map((line) => `> ${line}`)
    .join("\n");
  return `\n\n${attribution}\n${quoted}`;
}

/**
 * Build the prefilled subject and quote for a forward.
 *
 * Deliberately carries no In-Reply-To/References: a forward goes to
 * someone new, not into the sender's own conversation, so it starts a
 * thread of its own rather than joining theirs.
 */
export function buildForward(source: MessageDetail): ForwardDraft {
  const base = source.subject ?? "(no subject)";
  const subject = /^fwd?:/i.test(base) ? base : `Fwd: ${base}`;

  const attribution = [
    "---------- Forwarded message ----------",
    `From: ${source.from_addr ?? "unknown"}`,
    `Date: ${formatFullDate(source.received_at)}`,
    `Subject: ${source.subject ?? "(no subject)"}`,
    `To: ${(source.to_addrs ?? []).join(", ")}`,
  ].join("\n");

  return {
    subject,
    quotedText: quotedPlainText(source, attribution),
    attribution,
  };
}

/** Build the prefilled recipients, subject, threading headers and quote
 * for a reply. */
export function buildReply(
  source: MessageDetail,
  ownEmail: string,
  mode: "reply" | "reply-all",
): ReplyDraft {
  const senderEmail = extractEmail(source.from_addr);
  const exclude = new Set([ownEmail.toLowerCase()]);
  const to = dedupeExcluding(senderEmail ? [senderEmail] : [], new Set());

  let cc: string[] = [];
  if (mode === "reply-all") {
    const others = [...(source.to_addrs ?? []), ...(source.cc_addrs ?? [])].map(
      (a) => extractEmail(a),
    );
    exclude.add(senderEmail.toLowerCase());
    cc = dedupeExcluding(others, exclude);
  }

  const references = [...(source.references ?? [])];
  if (source.message_id) references.push(source.message_id);

  const attribution = replyAttribution(source);

  return {
    to,
    cc,
    subject: subjectWithPrefix(source.subject),
    quotedText: quotedPlainText(source, attribution),
    attribution,
    inReplyTo: source.message_id ?? undefined,
    references: references.length > 0 ? references : undefined,
  };
}
