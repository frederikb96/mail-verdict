/** Recipient, subject and threading-header derivation for reply/reply-all. */

import { extractEmail, extractSenderName, formatFullDate } from "@/lib/format";
import type { MessageDetail } from "@/types/api";

export interface ReplyDraft {
  to: string[];
  cc: string[];
  subject: string;
  bodyText: string;
  inReplyTo?: string;
  references?: string[];
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

function quoteBody(source: MessageDetail): string {
  const sender = extractSenderName(source.from_addr);
  const date = formatFullDate(source.received_at);
  const body = source.body_text ?? "";
  const quoted = body
    .split("\n")
    .map((line) => `> ${line}`)
    .join("\n");
  return `\n\nOn ${date}, ${sender} wrote:\n${quoted}`;
}

/** Build the prefilled recipients, subject and threading headers for a reply. */
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

  return {
    to,
    cc,
    subject: subjectWithPrefix(source.subject),
    bodyText: quoteBody(source),
    inReplyTo: source.message_id ?? undefined,
    references: references.length > 0 ? references : undefined,
  };
}
