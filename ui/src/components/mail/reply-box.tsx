"use client";

import { useState } from "react";
import { Forward, Loader2, Reply, ReplyAll } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ComposeForm } from "@/components/mail/compose-form";
import { buildForward, buildReply } from "@/lib/reply";
import { api } from "@/lib/api";
import type { MessageDetail } from "@/types/api";

interface ReplyBoxProps {
  /** The message being replied to — normally the last one in the thread. */
  source: MessageDetail;
  ownEmail: string;
}

/** Downloads a message's attachments and hands them back as Files, ready
 * to attach to a new outbox row -- forwarding carries the originals along
 * rather than making the sender re-attach them by hand. */
async function fetchAttachmentsAsFiles(source: MessageDetail): Promise<File[]> {
  const files = await Promise.all(
    source.attachments.map(async (att) => {
      const res = await fetch(api.mails.attachmentUrl(source.id, att.id));
      const blob = await res.blob();
      return new File([blob], att.filename ?? "attachment", {
        type: att.content_type ?? blob.type,
      });
    }),
  );
  return files;
}

/** Collapsed reply/reply-all/forward buttons that expand into an inline compose form. */
export function ReplyBox({ source, ownEmail }: ReplyBoxProps) {
  const [mode, setMode] = useState<"reply" | "reply-all" | "forward" | null>(null);
  const [forwardAttachments, setForwardAttachments] = useState<File[] | null>(null);
  const [loadingAttachments, setLoadingAttachments] = useState(false);

  const startForward = () => {
    if (source.attachments.length === 0) {
      setForwardAttachments([]);
      setMode("forward");
      return;
    }
    setLoadingAttachments(true);
    setMode("forward");
    fetchAttachmentsAsFiles(source)
      .then(setForwardAttachments)
      .finally(() => setLoadingAttachments(false));
  };

  const reset = () => {
    setMode(null);
    setForwardAttachments(null);
  };

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
        <Button variant="outline" size="sm" onClick={startForward}>
          <Forward className="mr-1 h-3.5 w-3.5" />
          Forward
        </Button>
      </div>
    );
  }

  if (mode === "forward") {
    if (loadingAttachments || forwardAttachments === null) {
      return (
        <div className="flex items-center gap-2 border-t p-3 text-sm text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Preparing forward...
        </div>
      );
    }
    const draft = buildForward(source);
    return (
      <div className="border-t p-3">
        <ComposeForm
          accountId={source.account_id}
          defaultSubject={draft.subject}
          defaultBody={draft.bodyText}
          initialAttachments={forwardAttachments}
          compact
          onDone={reset}
        />
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
        onDone={reset}
      />
    </div>
  );
}
