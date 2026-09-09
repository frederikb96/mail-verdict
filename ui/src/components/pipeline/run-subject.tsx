"use client";

import { Truncate } from "@/components/ui/truncate";
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
    return <Truncate text={msgKey} className="text-muted-foreground" />;
  }
  if (!mail) {
    return <Truncate text="Loading…" className="text-muted-foreground" />;
  }
  return (
    <Truncate
      text={mail.subject || "(no subject)"}
      className={mail.subject ? undefined : "text-muted-foreground"}
    />
  );
}
