"use client";

import { useRef, useState } from "react";
import { ArrowLeft, FileEdit, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ComposeForm, type ComposeFormControls } from "@/components/mail/compose-form";
import { DiscardChangesDialog } from "@/components/mail/discard-changes-dialog";
import { useMessageQuote } from "@/hooks/use-mails";
import { useIdentities } from "@/hooks/use-identities";
import { matchIdentity } from "@/lib/identities";
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
 *
 * The draft's body is reopened through GET /api/messages/:id/quote rather
 * than mail.body_html directly -- that field is display-shaped (cid:
 * images rewritten to local attachment URLs, blocked remote images
 * marked with data-x-src), neither of which means anything to a message
 * being edited and resent. The quote endpoint's own outbound sanitiser
 * pass is exactly the "make this safe to send again" step a reopened
 * draft needs too, and it is idempotent over content that already went
 * through it once when this draft was first saved.
 */
export function DraftEditor({ mail, onDone }: DraftEditorProps) {
  const toAddrs = Array.isArray(mail.to_addrs) ? (mail.to_addrs as string[]) : [];
  const ccAddrs = Array.isArray(mail.cc_addrs) ? (mail.cc_addrs as string[]) : [];
  const bccAddrs = Array.isArray(mail.bcc_addrs) ? (mail.bcc_addrs as string[]) : [];

  const { data: quote, isLoading } = useMessageQuote(mail.id);
  const { data: identities } = useIdentities(mail.account_id);
  const defaultIdentityId = matchIdentity([mail.from_addr], identities);

  const [isDirty, setIsDirty] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const controlsRef = useRef<ComposeFormControls | null>(null);

  const attemptBack = () => (isDirty ? setConfirming(true) : onDone());

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b p-4">
        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={attemptBack} title="Back">
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <FileEdit className="h-4 w-4 text-muted-foreground" />
        <h2 className="text-lg font-semibold leading-tight">Editing draft</h2>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {isLoading || !quote ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Loading draft...
          </div>
        ) : (
          <ComposeForm
            accountId={mail.account_id}
            defaultTo={toAddrs}
            defaultCc={ccAddrs}
            defaultBcc={bccAddrs}
            defaultSubject={mail.subject ?? ""}
            defaultIdentityId={defaultIdentityId}
            defaultBodyHtml={quote.html}
            inReplyTo={mail.in_reply_to ?? undefined}
            references={mail.references ?? undefined}
            replacesMessageId={mail.id}
            onDone={onDone}
            onDirtyChange={setIsDirty}
            onControlsReady={(controls) => {
              controlsRef.current = controls;
            }}
          />
        )}
      </div>
      <DiscardChangesDialog
        open={confirming}
        onOpenChange={setConfirming}
        onDiscard={() => {
          setConfirming(false);
          onDone();
        }}
        onSaveDraft={() => {
          setConfirming(false);
          controlsRef.current?.saveDraft();
        }}
      />
    </div>
  );
}
