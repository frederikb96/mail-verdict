"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, Paperclip, Send, Save, X, ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { RecipientField } from "@/components/contacts/recipient-field";
import { MailEditorLazy } from "@/components/mail/editor/mail-editor-lazy";
import { buildQuotedMessageHtml } from "@/components/mail/editor/quoted-message-node";
import { rewriteInlineImageSrcs } from "@/components/mail/editor/inline-image";
import type { MailEditorHandle } from "@/components/mail/editor/mail-editor";
import {
  ComposeResizeControlsBar,
  useComposeResize,
} from "@/components/mail/editor/resizable-panel";
import { DiscardChangesDialog } from "@/components/mail/discard-changes-dialog";
import { useCreateOutbox } from "@/hooks/use-outbox";
import { useIdentities } from "@/hooks/use-identities";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import type { OutboxCreateRequest } from "@/types/api";

/** A message quoted or forwarded into the composer -- see reply-box.tsx,
 * which is what fetches messages/:id/quote and builds this. */
export interface ComposeQuote {
  html: string;
  attribution: string;
}

/** The subset of ComposeForm's submit machinery a host surface (the
 * dialog, the reply box, the draft editor) needs to drive its own close
 * button -- saving as a draft before actually closing. */
export interface ComposeFormControls {
  saveDraft: () => void;
}

interface ComposeFormProps {
  accountId: string;
  defaultTo?: string[];
  defaultCc?: string[];
  defaultBcc?: string[];
  defaultSubject?: string;
  /** Which identity to preselect -- a reply resolves this to whichever
   * of the account's identities the original message was addressed to;
   * a fresh compose leaves it unset and falls back to the account's
   * starred default, the same way the API itself does when no
   * identity_id is sent at all. */
  defaultIdentityId?: string;
  /** A previously-saved draft's full HTML body, reopened as-is --
   * mutually exclusive with `quote` (a draft that itself quoted
   * something already has that quote embedded in this HTML). */
  defaultBodyHtml?: string;
  /** A reply or forward's quoted original, embedded as the editor's
   * initial content alongside an empty paragraph to type into. */
  quote?: ComposeQuote;
  /** The `> `-prefixed plain-text form of `quote`, appended to the
   * editor's own markdown export to build body_text. */
  quotedText?: string;
  inReplyTo?: string;
  references?: string[];
  /** The messages.id of a draft being edited or sent from -- both buttons
   * insert with this set, so PostIMAP replaces the draft in place instead
   * of leaving it behind as a duplicate. */
  replacesMessageId?: string;
  /** Pre-attached files, e.g. a forward carrying the original's attachments. */
  initialAttachments?: File[];
  /** Inline reply box styling instead of a standalone dialog form. */
  compact?: boolean;
  onDone: () => void;
  /** Whether there is anything here that would be lost by closing
   * without saving -- the host surface uses this to decide whether
   * closing needs to ask first. */
  onDirtyChange?: (dirty: boolean) => void;
  onControlsReady?: (controls: ComposeFormControls) => void;
  /** The resizable panel's maximize toggle, one level up -- "fill the
   * window" is a different CSS change for a modal dialog than for a
   * panel anchored at the bottom of the reading pane, so each host
   * decides what its own maximized layout looks like. */
  onMaximizedChange?: (maximized: boolean) => void;
}

/** Shared body for the new-mail dialog, the inline reply box, and the draft editor. */
export function ComposeForm({
  accountId,
  defaultTo = [],
  defaultCc = [],
  defaultBcc = [],
  defaultSubject = "",
  defaultIdentityId,
  defaultBodyHtml,
  quote,
  quotedText = "",
  inReplyTo,
  references,
  replacesMessageId,
  initialAttachments = [],
  compact = false,
  onDone,
  onDirtyChange,
  onControlsReady,
  onMaximizedChange,
}: ComposeFormProps) {
  const resize = useComposeResize();
  useEffect(() => {
    onMaximizedChange?.(resize.isMaximized);
    // onMaximizedChange is expected stable, the same reasoning
    // onDirtyChange's own effect below gives -- only isMaximized itself
    // should re-trigger this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resize.isMaximized]);

  const [to, setTo] = useState<string[]>(defaultTo);
  const [cc, setCc] = useState<string[]>(defaultCc);
  const [bcc, setBcc] = useState<string[]>(defaultBcc);
  const [showCcBcc, setShowCcBcc] = useState(defaultCc.length > 0 || defaultBcc.length > 0);
  const [subject, setSubject] = useState(defaultSubject);
  const [attachments, setAttachments] = useState<File[]>(initialAttachments);
  const [identityId, setIdentityId] = useState("");
  const [bodyDirty, setBodyDirty] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // A ref, not the mutation's own isPending: that is react-query's state as
  // of the last render, so two submits reaching this handler in the same
  // tick (a fast double-click or double Enter, before React has re-rendered
  // the disabled button) would both read it as false and both fire. A ref
  // is read and written synchronously, with no render in between, so the
  // second one sees what the first just set.
  const submittingRef = useRef(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const { data: identities } = useIdentities(accountId);
  const createOutbox = useCreateOutbox();
  const { push: pushToast } = useToast();

  // Never `undefined`, per the trap that costs the most hours in this
  // codebase: a Select whose value starts `undefined` decides on that
  // first render that it is uncontrolled, and never picks up a real
  // value set later once the identities query resolves.
  const effectiveIdentityId =
    identityId || defaultIdentityId || identities?.find((i) => i.is_default)?.id || "";

  const editorHandle = useRef<MailEditorHandle | null>(null);

  const initialHtml =
    defaultBodyHtml ?? (quote ? `<p></p>${buildQuotedMessageHtml(quote)}` : "");

  const initialSnapshot = useRef({
    to: defaultTo,
    cc: defaultCc,
    bcc: defaultBcc,
    subject: defaultSubject,
    attachmentNames: initialAttachments.map((f) => f.name),
  });

  const isDirty =
    bodyDirty ||
    JSON.stringify(to) !== JSON.stringify(initialSnapshot.current.to) ||
    JSON.stringify(cc) !== JSON.stringify(initialSnapshot.current.cc) ||
    JSON.stringify(bcc) !== JSON.stringify(initialSnapshot.current.bcc) ||
    subject !== initialSnapshot.current.subject ||
    JSON.stringify(attachments.map((f) => f.name)) !==
      JSON.stringify(initialSnapshot.current.attachmentNames);

  useEffect(() => {
    onDirtyChange?.(isDirty);
    // Deliberately every render rather than a narrower dependency list --
    // isDirty is a cheap derived value, not a reference the caller needs
    // held stable, and listing its inputs one by one here would just be
    // isDirty's own definition duplicated.
  });

  /** The request body plus the full attachment file list -- a pasted
   * image still referenced in the document (editorHandle's own
   * getInlineImages(), which already excludes one that was since deleted)
   * is appended after the picked-file attachments, in the same order its
   * content id is appended to inline_attachment_content_ids, so the two
   * lists line up positionally the way the API expects. */
  const buildRequest = (
    kind: "send" | "draft",
  ): { data: OutboxCreateRequest; files: File[] } => {
    const empty = editorHandle.current?.isEmpty() ?? true;
    const rawHtml = empty ? undefined : editorHandle.current?.getHTML();
    const inlineImages = empty ? [] : (editorHandle.current?.getInlineImages() ?? []);
    // The blob: src is only ever a display convenience for this browser
    // tab -- the recipient's client resolves cid: against the matching
    // inline attachment instead.
    const html = rawHtml ? rewriteInlineImageSrcs(rawHtml) : rawHtml;
    const markdown = empty ? "" : (editorHandle.current?.getMarkdown() ?? "");
    return {
      data: {
        account_id: accountId,
        kind,
        to,
        cc,
        bcc,
        subject,
        body_text: `${markdown}${quotedText}`,
        body_html: html,
        in_reply_to: inReplyTo,
        references,
        identity_id: effectiveIdentityId || undefined,
        replaces_message_id: replacesMessageId,
        inline_attachment_content_ids: [
          ...attachments.map(() => null),
          ...inlineImages.map((image) => image.contentId),
        ],
      },
      files: [...attachments, ...inlineImages.map((image) => image.file)],
    };
  };

  const submit = (kind: "send" | "draft") => {
    // Checked and set before anything else so a second call reaching this
    // function while the first is still in flight -- whichever kind either
    // one is -- returns immediately rather than mutating a second time.
    if (submittingRef.current) return;
    const { data, files } = buildRequest(kind);
    if (kind === "send" && data.to.length === 0) {
      pushToast("Add at least one recipient", "warning");
      return;
    }
    submittingRef.current = true;
    setIsSubmitting(true);
    createOutbox.mutate(
      { data, attachments: files },
      {
        onSuccess: (result) => {
          // A staged send (undo window above zero) is reported through
          // the undo banner instead of a toast -- the two would say the
          // same thing twice, and the banner is also where cancelling it
          // lives.
          if (!("send_after" in result)) {
            pushToast(
              kind === "send" ? "Message queued for sending" : "Draft saved",
              "success",
            );
          }
          onDone();
        },
        onError: (err) => {
          pushToast(`Failed to queue message: ${err.message}`, "error", 0);
        },
        onSettled: () => {
          submittingRef.current = false;
          setIsSubmitting(false);
        },
      },
    );
  };

  const submitRef = useRef(submit);
  submitRef.current = submit;

  useEffect(() => {
    onControlsReady?.({ saveDraft: () => submitRef.current("draft") });
    // onControlsReady is called once -- submitRef.current is always the
    // latest submit, so the host surface's close button never sends a
    // stale snapshot of the form even though this effect itself never
    // re-runs.
  }, []);

  return (
    <div
      className={cn(
        "flex flex-col gap-2",
        resize.isMaximized && "h-full min-h-0 flex-1",
      )}
    >
      {!compact && <RecipientField value={to} onChange={setTo} placeholder="To" />}
      {compact && (
        <div className="grid grid-cols-[auto_1fr] items-center gap-2">
          <span className="text-xs text-muted-foreground">To</span>
          <RecipientField value={to} onChange={setTo} placeholder="Recipients" />
        </div>
      )}

      {!showCcBcc && (
        <button
          type="button"
          className="flex w-fit items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          onClick={() => setShowCcBcc(true)}
        >
          Cc/Bcc
          <ChevronDown className="h-3 w-3" />
        </button>
      )}
      {showCcBcc && (
        <>
          <RecipientField value={cc} onChange={setCc} placeholder="Cc" />
          <RecipientField value={bcc} onChange={setBcc} placeholder="Bcc" />
        </>
      )}

      {!compact && (
        <Input
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          placeholder="Subject"
        />
      )}

      {identities && identities.length > 1 && (
        <div className="grid grid-cols-[auto_1fr] items-center gap-2">
          <span className="text-xs text-muted-foreground">From</span>
          <Select value={effectiveIdentityId} onValueChange={(v) => setIdentityId(v ?? "")}>
            <SelectTrigger className="h-8">
              <SelectValue placeholder="From address">
                {(v: string) => {
                  const found = identities.find((i) => i.id === v);
                  return found ? (found.display_name || found.address) : "From address";
                }}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {identities.map((identity) => (
                <SelectItem key={identity.id} value={identity.id}>
                  {identity.display_name || identity.address}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}

      <ComposeResizeControlsBar controls={resize} />
      <MailEditorLazy
        key={initialHtml}
        initialHtml={initialHtml}
        autoFocus={compact}
        compact={compact}
        heightPx={resize.isMaximized ? undefined : resize.heightPx}
        fillHeight={resize.isMaximized}
        onDirtyChange={setBodyDirty}
        onReady={(handle) => {
          editorHandle.current = handle;
        }}
      />

      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {attachments.map((file, i) => (
            <Badge key={`${file.name}-${i}`} variant="outline" className="gap-1">
              <Paperclip className="h-3 w-3" />
              {file.name}
              <button
                type="button"
                onClick={() =>
                  setAttachments((prev) => prev.filter((_, idx) => idx !== i))
                }
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}

      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              setAttachments((prev) => [...prev, ...files]);
              e.target.value = "";
            }}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() => fileInputRef.current?.click()}
            title="Attach files"
          >
            <Paperclip className="h-4 w-4" />
          </Button>
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={isSubmitting}
            onClick={() => submit("draft")}
          >
            <Save className="mr-1 h-3.5 w-3.5" />
            Save draft
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={isSubmitting}
            onClick={() => submit("send")}
          >
            {isSubmitting ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Send className="mr-1 h-3.5 w-3.5" />
            )}
            Send
          </Button>
        </div>
      </div>
    </div>
  );
}

interface ComposeCloseButtonProps {
  isDirty: boolean;
  onDiscard: () => void;
  saveDraft: (() => void) | null;
  isSaving?: boolean;
  className?: string;
}

/**
 * A close control that asks first when there is unsaved work -- shared by
 * the reply box (which had no close control at all) and the draft
 * editor's existing Back arrow, so both surfaces gain the same prompt
 * with one implementation. The compose dialog's own close button already
 * routes through Base UI's onOpenChange instead (see compose-dialog.tsx),
 * since that also has to cover Escape and an outside click.
 */
export function ComposeCloseButton({
  isDirty,
  onDiscard,
  saveDraft,
  isSaving = false,
  className,
}: ComposeCloseButtonProps) {
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <Button
        variant="ghost"
        size="icon-sm"
        className={className}
        title="Close"
        onClick={() => (isDirty ? setConfirming(true) : onDiscard())}
      >
        <X className="h-3.5 w-3.5" />
      </Button>
      <DiscardChangesDialog
        open={confirming}
        onOpenChange={setConfirming}
        onDiscard={() => {
          setConfirming(false);
          onDiscard();
        }}
        onSaveDraft={() => {
          setConfirming(false);
          saveDraft?.();
        }}
        isSaving={isSaving}
      />
    </>
  );
}
