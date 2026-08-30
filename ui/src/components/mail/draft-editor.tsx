"use client";

import { ArrowLeft, FileEdit } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ComposeForm } from "@/components/mail/compose-form";
import type { MessageDetail } from "@/types/api";

interface DraftEditorProps {
  mail: MessageDetail;
  onDone: () => void;
}

/**
 * Reopens a saved draft for editing, in place of the normal thread view.
 *
 * Save or Send both insert a new outbox row naming this draft's own
 * messages.id as replaces_message_id, so PostIMAP appends the replacement
 * and only then removes this one -- one edit, not an expunge plus a
 * separate create. The superseded message can still be visible in Drafts
 * for the width of that gap; rendering from this component's own state
 * (not by re-reading the mailbox) is what makes that invisible here.
 */
export function DraftEditor({ mail, onDone }: DraftEditorProps) {
  const toAddrs = Array.isArray(mail.to_addrs) ? (mail.to_addrs as string[]) : [];
  const ccAddrs = Array.isArray(mail.cc_addrs) ? (mail.cc_addrs as string[]) : [];
  const bccAddrs = Array.isArray(mail.bcc_addrs) ? (mail.bcc_addrs as string[]) : [];

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b p-4">
        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={onDone} title="Back">
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <FileEdit className="h-4 w-4 text-muted-foreground" />
        <h2 className="text-lg font-semibold leading-tight">Editing draft</h2>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        <ComposeForm
          accountId={mail.account_id}
          defaultTo={toAddrs}
          defaultCc={ccAddrs}
          defaultBcc={bccAddrs}
          defaultSubject={mail.subject ?? ""}
          defaultBody={mail.body_text ?? ""}
          inReplyTo={mail.in_reply_to ?? undefined}
          references={mail.references ?? undefined}
          replacesMessageId={mail.id}
          onDone={onDone}
        />
      </div>
    </div>
  );
}
