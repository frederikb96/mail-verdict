"use client";

import { useMailDetail } from "@/hooks/use-mails";

/**
 * A run's subject line, looked up lazily from the message it acted on.
 * Falls back to the dedup key (the message's `Message-ID` header, or a
 * content hash for mail that never had one) when the message is gone or
 * still loading -- always shows something rather than nothing.
 */
export function RunSubject({
  messageId,
  msgKey,
}: {
  messageId: string | null;
  msgKey: string;
}) {
  const { data: mail, isError } = useMailDetail(messageId);

  if (!messageId || isError) {
    return <span className="truncate text-muted-foreground">{msgKey}</span>;
  }
  if (!mail) {
    return <span className="truncate text-muted-foreground">Loading…</span>;
  }
  return (
    <span className="truncate">
      {mail.subject || <span className="text-muted-foreground">(no subject)</span>}
    </span>
  );
}
