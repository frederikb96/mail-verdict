"use client";

import { useEffect, useRef, useState } from "react";
import { useSetAtom } from "jotai";
import { Forward, Loader2, Reply, ReplyAll } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  ComposeCloseButton,
  ComposeForm,
  type ComposeFormControls,
} from "@/components/mail/compose-form";
import { buildForward, buildReply } from "@/lib/reply";
import { matchIdentity } from "@/lib/identities";
import { useIdentities } from "@/hooks/use-identities";
import { api } from "@/lib/api";
import { activeReplyDirtyForMailIdAtom } from "@/lib/atoms";
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
  const [quoteHtml, setQuoteHtml] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [isDirty, setIsDirty] = useState(false);
  const controlsRef = useRef<ComposeFormControls | null>(null);
  const setActiveReplyDirtyForMailId = useSetAtom(activeReplyDirtyForMailIdAtom);

  // Read by useMailAction: while this is dirty, a "leaves folder" action
  // taken on this same message from somewhere else (a row's own hover
  // control, a keyboard shortcut) must not clear the open selection --
  // that would unmount this box along with it, discarding whatever was
  // typed with no prompt at all. Cleared on unmount too, so a stale id
  // never outlives the box that set it.
  useEffect(() => {
    setActiveReplyDirtyForMailId(isDirty ? source.id : null);
    return () => setActiveReplyDirtyForMailId(null);
  }, [isDirty, source.id, setActiveReplyDirtyForMailId]);

  const { data: identities } = useIdentities(source.account_id);
  // A reply and a forward alike go out as whichever of the account's
  // identities the original was addressed to -- To before Cc, since that
  // is the order the header itself carries the message's recipients in.
  const defaultIdentityId = matchIdentity(
    [...(source.to_addrs ?? []), ...(source.cc_addrs ?? [])],
    identities,
  );

  const start = (next: "reply" | "reply-all" | "forward") => {
    setMode(next);
    const attachmentsNeeded = next === "forward" && source.attachments.length > 0;
    setLoading(true);
    Promise.all([
      attachmentsNeeded ? fetchAttachmentsAsFiles(source) : Promise.resolve([]),
      api.mails.quote(source.id),
    ])
      .then(([files, quote]) => {
        setForwardAttachments(files);
        setQuoteHtml(quote.html);
      })
      .finally(() => setLoading(false));
  };

  const reset = () => {
    setMode(null);
    setForwardAttachments(null);
    setQuoteHtml(null);
    setIsDirty(false);
  };

  if (!mode) {
    return (
      <div className="flex gap-2 border-t p-3">
        <Button variant="outline" size="sm" onClick={() => start("reply")}>
          <Reply className="mr-1 h-3.5 w-3.5" />
          Reply
        </Button>
        <Button variant="outline" size="sm" onClick={() => start("reply-all")}>
          <ReplyAll className="mr-1 h-3.5 w-3.5" />
          Reply all
        </Button>
        <Button variant="outline" size="sm" onClick={() => start("forward")}>
          <Forward className="mr-1 h-3.5 w-3.5" />
          Forward
        </Button>
      </div>
    );
  }

  if (loading || forwardAttachments === null || quoteHtml === null) {
    return (
      <div className="flex items-center gap-2 border-t p-3 text-sm text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Preparing...
      </div>
    );
  }

  const common = (
    to: string[],
    cc: string[],
    subject: string,
    quotedText: string,
    attribution: string,
    inReplyTo: string | undefined,
    references: string[] | undefined,
  ) => (
    <div className="border-t p-3">
      <div className="mb-1 flex justify-end">
        <ComposeCloseButton
          isDirty={isDirty}
          onDiscard={reset}
          saveDraft={() => controlsRef.current?.saveDraft()}
        />
      </div>
      <ComposeForm
        accountId={source.account_id}
        defaultTo={to}
        defaultCc={cc}
        defaultSubject={subject}
        defaultIdentityId={defaultIdentityId}
        quote={{ html: quoteHtml, attribution }}
        quotedText={quotedText}
        inReplyTo={inReplyTo}
        references={references}
        initialAttachments={forwardAttachments}
        compact
        onDone={reset}
        onDirtyChange={setIsDirty}
        onControlsReady={(controls) => {
          controlsRef.current = controls;
        }}
      />
    </div>
  );

  if (mode === "forward") {
    const draft = buildForward(source);
    return common([], [], draft.subject, draft.quotedText, draft.attribution, undefined, undefined);
  }

  const draft = buildReply(source, ownEmail, mode);
  return common(
    draft.to,
    draft.cc,
    draft.subject,
    draft.quotedText,
    draft.attribution,
    draft.inReplyTo,
    draft.references,
  );
}
