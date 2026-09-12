"use client";

/** One message inside a thread: a collapsed summary row, or -- once
 * expanded -- its full header, verdict, body and attachments. The
 * reading pane renders one of these per message in the thread; this is
 * where per-message rendering concerns (the email body itself, images,
 * attachments) live, separate from the reading pane's own thread-level
 * header, action row and reply box. */

import { useState } from "react";
import {
  Paperclip,
  Download,
  Eye,
  ChevronRight,
  ChevronDown,
  Copy,
  Loader2,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { AttachmentPreviewDialog } from "@/components/mail/attachment-preview-dialog";
import { EmailRenderer } from "@/components/mail/email-renderer";
import { ImageBanner } from "@/components/mail/image-banner";
import { TruncatedBanner } from "@/components/mail/truncated-banner";
import { InvitationCard } from "@/components/mail/invitation-card";
import { useContactByEmail } from "@/hooks/use-contacts";
import { useToast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import {
  extractSenderName,
  extractEmail,
  formatFullDate,
  formatRelativeDate,
  formatSize,
} from "@/lib/format";
import type { AttachmentSummary, MessageDetail } from "@/types/api";

const CALENDAR_CONTENT_TYPES = ["text/calendar", "application/ics"];

function hasCalendarAttachment(mail: MessageDetail): boolean {
  return mail.attachments.some(
    (att) => att.content_type && CALENDAR_CONTENT_TYPES.includes(att.content_type),
  );
}

/** Copies one bare address -- the shape a compose recipient field accepts
 * pasted -- and keeps the click from reaching the header's fold. */
function CopyableAddress({
  address,
  onCopy,
  testId,
  children,
}: {
  address: string;
  onCopy: (text: string) => void;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      title={`Copy ${address}`}
      onClick={(e) => {
        e.stopPropagation();
        onCopy(address);
      }}
      className="rounded-sm text-left hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
    >
      {children}
    </button>
  );
}

/** One header line of recipients: each copies on its own, and the trailing
 * control copies the whole line comma-separated, ready to paste into a
 * recipient field. */
function RecipientLine({
  label,
  name,
  addrs,
  onCopy,
}: {
  label: string;
  name: string;
  addrs: string[] | null;
  onCopy: (text: string) => void;
}) {
  if (!addrs || addrs.length === 0) return null;
  const emails = addrs.map(extractEmail);
  return (
    <div className="text-xs text-muted-foreground">
      {label}{" "}
      {addrs.map((addr, i) => (
        <span key={`${addr}-${i}`}>
          {i > 0 && ", "}
          <CopyableAddress address={emails[i]} onCopy={onCopy} testId="thread-message-recipient">
            {addr}
          </CopyableAddress>
        </span>
      ))}
      <button
        type="button"
        aria-label={`Copy all ${name} addresses`}
        title={`Copy all ${name} addresses`}
        onClick={(e) => {
          e.stopPropagation();
          onCopy(emails.join(", "));
        }}
        className="ml-1 inline-flex rounded-sm align-middle hover:text-foreground"
      >
        <Copy className="h-3 w-3" />
      </button>
    </div>
  );
}

export function ThreadMessage({
  mail,
  expanded,
  onToggle,
  onLoadImages,
  searchQuery,
  activeMatchIndex,
  onMatchCountChange,
}: {
  mail: MessageDetail;
  expanded: boolean;
  onToggle: () => void;
  onLoadImages: () => void;
  /** The reading pane's own in-message find -- see ReadingPane, which
   * scopes these to whichever message is actually open rather than
   * passing them to every message in the thread. */
  searchQuery?: string;
  activeMatchIndex?: number;
  onMatchCountChange?: (count: number) => void;
}) {
  const [previewAttachment, setPreviewAttachment] = useState<AttachmentSummary | null>(null);
  const { push: pushToast } = useToast();
  const senderName = extractSenderName(mail.from_addr);
  const senderEmail = extractEmail(mail.from_addr);

  const { data: senderContact } = useContactByEmail(senderEmail);
  // An embedded photo is a data: URI already in the mirror -- free to
  // render. A URL photo is a third party's address, exactly like a
  // remote image in the message body, so it only renders once this
  // sender is on the same allowlist that gates the body's own images --
  // never fetched unconditionally. "Load for this message" below is
  // deliberately not one of these triggers: it restores the body's own
  // already-fetched content for one viewing, not a new fetch against a
  // sender nothing vouches for.
  const imagesAllowed = mail.images_allowed;
  const senderPhotoUrl =
    senderContact?.photo?.kind === "embedded"
      ? senderContact.photo.url
      : senderContact?.photo?.kind === "url" && imagesAllowed
        ? senderContact.photo.url
        : null;

  const copy = (text: string) => {
    const written = navigator.clipboard?.writeText(text) ?? Promise.reject();
    written.then(
      () => pushToast(`Copied ${text}`, "info", 2000),
      () => pushToast("Could not copy to the clipboard", "error"),
    );
  };

  if (!expanded) {
    return (
      <div data-testid="thread-message" data-message-id={mail.id}>
      <button
        type="button"
        data-testid="thread-message-header"
        onClick={onToggle}
        className="flex w-full items-center gap-3 border-b px-4 py-2.5 text-left hover:bg-accent/50"
      >
        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <InitialsAvatar name={senderName} size="sm" photoUrl={senderPhotoUrl} colorSeed={senderEmail || senderName} />
        <span
          className={mail.is_seen ? "font-medium" : "font-semibold"}
        >
          {senderName}
        </span>
        {mail.pending_sync && (
          <Loader2 className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />
        )}
        <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">
          {mail.snippet}
        </span>
        <span className="shrink-0 text-xs text-muted-foreground">
          {formatRelativeDate(mail.received_at)}
        </span>
      </button>
      </div>
    );
  }

  return (
    <div
      data-testid="thread-message"
      data-message-id={mail.id}
      className="flex flex-col border-b"
    >
      {/* Only the header's blank area folds the message: the sender and
          each recipient copy their address, and the avatar and date do
          nothing, so reaching for an address never collapses what is being
          read. Selecting text is not a fold either. The chevron is the
          keyboard's way to the same toggle. */}
      <div
        data-testid="thread-message-header"
        onClick={() => {
          if (window.getSelection()?.toString()) return;
          onToggle();
        }}
        className="flex cursor-pointer items-start justify-between gap-4 px-4 pb-2 pt-3"
      >
        <div className="flex min-w-0 items-start gap-2">
          <div className="cursor-default" onClick={(e) => e.stopPropagation()}>
            <InitialsAvatar name={senderName} photoUrl={senderPhotoUrl} colorSeed={senderEmail || senderName} />
          </div>
          <div className="flex min-w-0 flex-col gap-0.5">
            <span>
              <CopyableAddress
                address={senderEmail}
                onCopy={copy}
                testId="thread-message-sender"
              >
                <span className="font-medium">{senderName}</span>{" "}
                <span className="text-xs text-muted-foreground">&lt;{senderEmail}&gt;</span>
              </CopyableAddress>
            </span>
            <RecipientLine label="to" name="To" addrs={mail.to_addrs} onCopy={copy} />
            <RecipientLine label="Cc:" name="Cc" addrs={mail.cc_addrs} onCopy={copy} />
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
          {mail.pending_sync && <Loader2 className="h-3 w-3 animate-spin" />}
          <span
            data-testid="thread-message-date"
            className="cursor-text"
            onClick={(e) => e.stopPropagation()}
          >
            {formatFullDate(mail.received_at)}
          </span>
          <button
            type="button"
            aria-label="Collapse message"
            aria-expanded
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
            className="rounded-sm hover:text-foreground"
          >
            <ChevronDown className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {mail.verdict && (
        <div className="flex items-center gap-2 px-4 pb-3 text-xs">
          <Badge variant={mail.verdict.is_spam ? "destructive" : "outline"}>
            {mail.verdict.is_spam ? "Flagged as spam" : "Not spam"}
          </Badge>
          <span className="text-muted-foreground">
            {mail.verdict.reasoning}
          </span>
        </div>
      )}

      {hasCalendarAttachment(mail) && <InvitationCard messageId={mail.id} />}

      {mail.is_truncated && <TruncatedBanner />}

      {!mail.is_truncated && (
        <ImageBanner
          accountId={mail.account_id}
          senderEmail={senderEmail}
          senderDomain={senderEmail?.split("@")[1] ?? null}
          imagesAllowed={imagesAllowed}
          hasBlockedImages={mail.has_blocked_images}
          onLoadForMessage={onLoadImages}
        />
      )}

      {!mail.is_truncated && (
        <div className="min-h-0">
          <EmailRenderer
            html={mail.body_html}
            plainText={mail.body_text}
            messageId={mail.id}
            searchQuery={searchQuery}
            activeMatchIndex={activeMatchIndex}
            onMatchCountChange={onMatchCountChange}
          />
        </div>
      )}

      {mail.attachments.length > 0 && (
        <div className="px-4 pb-3">
          <div className="mb-2 flex items-center gap-2 text-sm font-medium">
            <Paperclip className="h-4 w-4" />
            {mail.attachments.length} attachment
            {mail.attachments.length > 1 ? "s" : ""}
          </div>
          <div className="flex flex-wrap gap-2">
            {mail.attachments.map((att) => (
              <div
                key={att.id}
                className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
              >
                <Paperclip className="h-3 w-3 text-muted-foreground" />
                <span className="max-w-40 truncate">
                  {att.filename ?? "Attachment"}
                </span>
                {att.size_bytes !== null && (
                  <span className="text-xs text-muted-foreground">
                    ({formatSize(att.size_bytes)})
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => setPreviewAttachment(att)}
                  className="ml-1"
                  title={`Preview ${att.filename ?? "attachment"}`}
                  aria-label={`Preview ${att.filename ?? "attachment"}`}
                >
                  <Eye className="h-3 w-3 text-muted-foreground hover:text-foreground" />
                </button>
                <a
                  href={api.mails.attachmentUrl(mail.id, att.id)}
                  download={att.filename ?? "attachment"}
                  title={`Download ${att.filename ?? "attachment"}`}
                  aria-label={`Download ${att.filename ?? "attachment"}`}
                >
                  <Download className="h-3 w-3 text-muted-foreground hover:text-foreground" />
                </a>
              </div>
            ))}
          </div>
        </div>
      )}

      {previewAttachment && (
        <AttachmentPreviewDialog
          messageId={mail.id}
          attachment={previewAttachment}
          onOpenChange={(open) => {
            if (!open) setPreviewAttachment(null);
          }}
        />
      )}
    </div>
  );
}
