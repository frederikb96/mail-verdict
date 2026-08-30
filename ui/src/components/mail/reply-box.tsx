"use client";

import { useState } from "react";
import { Reply, ReplyAll } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ComposeForm } from "@/components/mail/compose-form";
import { buildReply } from "@/lib/reply";
import type { MessageDetail } from "@/types/api";

interface ReplyBoxProps {
  /** The message being replied to — normally the last one in the thread. */
  source: MessageDetail;
  ownEmail: string;
}

/** Collapsed reply/reply-all buttons that expand into an inline compose form. */
export function ReplyBox({ source, ownEmail }: ReplyBoxProps) {
  const [mode, setMode] = useState<"reply" | "reply-all" | null>(null);

  if (!mode) {
    return (
      <div className="flex gap-2 border-t p-3">
        <Button variant="outline" size="sm" onClick={() => setMode("reply")}>
          <Reply className="mr-1 h-3.5 w-3.5" />
          Reply
        </Button>
        <Button variant="outline" size="sm" onClick={() => setMode("reply-all")}>
          <ReplyAll className="mr-1 h-3.5 w-3.5" />
          Reply all
        </Button>
      </div>
    );
  }

  const draft = buildReply(source, ownEmail, mode);

  return (
    <div className="border-t p-3">
      <ComposeForm
        accountId={source.account_id}
        defaultTo={draft.to}
        defaultCc={draft.cc}
        defaultSubject={draft.subject}
        defaultBody={draft.bodyText}
        inReplyTo={draft.inReplyTo}
        references={draft.references}
        compact
        onDone={() => setMode(null)}
      />
    </div>
  );
}
