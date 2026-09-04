"use client";

/** One message inside a thread: a collapsed summary row, or -- once
 * expanded -- its full header, body and per-message action bar. The
 * reading pane renders one of these per message in the thread; this is
 * where per-message rendering concerns (the email body itself, images,
 * attachments, spam feedback) live, separate from the reading pane's own
 * thread-level header and reply box. */

import {
  Paperclip,
  Download,
  FileDown,
  MailOpen,
  MailIcon,
  ChevronRight,
  ChevronDown,
  Loader2,
  ThumbsDown,
  ThumbsUp,
  Star,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { EmailRenderer } from "@/components/mail/email-renderer";
import { ImageBanner } from "@/components/mail/image-banner";
import { TruncatedBanner } from "@/components/mail/truncated-banner";
import { InvitationCard } from "@/components/mail/invitation-card";
import { api } from "@/lib/api";
import { useMailAction } from "@/hooks/use-mails";
import { useVerdictFeedback } from "@/hooks/use-verdicts";
import {
  extractSenderName,
  extractEmail,
  formatAddresses,
  formatFullDate,
  formatRelativeDate,
  formatSize,
} from "@/lib/format";
import type { MessageDetail } from "@/types/api";

const CALENDAR_CONTENT_TYPES = ["text/calendar", "application/ics"];

function hasCalendarAttachment(mail: MessageDetail): boolean {
  return mail.attachments.some(
    (att) => att.content_type && CALENDAR_CONTENT_TYPES.includes(att.content_type),
  );
}

export function ThreadMessage({
  mail,
  expanded,
  onToggle,
  imagesAllowedOverride,
  onLoadImages,
}: {
  mail: MessageDetail;
  expanded: boolean;
  onToggle: () => void;
  imagesAllowedOverride: boolean;
  onLoadImages: () => void;
}) {
  const mailAction = useMailAction();
  const verdictFeedback = useVerdictFeedback();
  const senderName = extractSenderName(mail.from_addr);
  const senderEmail = extractEmail(mail.from_addr);

  if (!expanded) {
    return (
      <button
        type="button"
        data-testid="thread-message-header"
        onClick={onToggle}
        className="flex w-full items-center gap-3 border-b px-4 py-2.5 text-left hover:bg-accent/50"
      >
        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <InitialsAvatar
          name={senderName}
          size="sm"
          email={senderEmail}
          imagesAllowed={mail.images_allowed}
        />
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
    );
  }

  return (
    <div className="flex flex-col border-b">
      <div className="flex flex-col gap-2 px-4 py-3">
        <button
          type="button"
          data-testid="thread-message-header"
          onClick={onToggle}
          className="flex items-start justify-between gap-4 text-left"
        >
          <div className="flex items-start gap-2">
            <InitialsAvatar
              name={senderName}
              email={senderEmail}
              imagesAllowed={mail.images_allowed}
            />
            <div className="flex flex-col gap-0.5">
              <span className="font-medium">{senderName}</span>
              <span className="text-xs text-muted-foreground">
                &lt;{senderEmail}&gt; to {formatAddresses(mail.to_addrs)}
              </span>
              {mail.cc_addrs && mail.cc_addrs.length > 0 && (
                <span className="text-xs text-muted-foreground">
                  Cc: {formatAddresses(mail.cc_addrs)}
                </span>
              )}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
            {mail.pending_sync && <Loader2 className="h-3 w-3 animate-spin" />}
            {formatFullDate(mail.received_at)}
            <ChevronDown className="h-3.5 w-3.5" />
          </div>
        </button>

        {mail.verdict && (
          <div className="flex items-center gap-2 text-xs">
            <Badge variant={mail.verdict.is_spam ? "destructive" : "outline"}>
              {mail.verdict.is_spam ? "Flagged as spam" : "Not spam"}
            </Badge>
            <span className="text-muted-foreground">
              {mail.verdict.reasoning}
            </span>
          </div>
        )}
      </div>

      {hasCalendarAttachment(mail) && <InvitationCard messageId={mail.id} />}

      {mail.is_truncated && <TruncatedBanner />}

      {!mail.is_truncated && (
        <ImageBanner
          accountId={mail.account_id}
          senderEmail={senderEmail}
          senderDomain={senderEmail?.split("@")[1] ?? null}
          imagesAllowed={mail.images_allowed || imagesAllowedOverride}
          hasBlockedImages={mail.has_blocked_images}
          onLoadForMessage={onLoadImages}
        />
      )}

      {!mail.is_truncated && (
        <div className="min-h-0">
          <EmailRenderer
            html={mail.body_html}
            plainText={mail.body_text}
            imagesAllowed={mail.images_allowed || imagesAllowedOverride}
            messageId={mail.id}
            supportsDarkMode={mail.supports_dark_mode}
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
                <a
                  href={api.mails.attachmentUrl(mail.id, att.id)}
                  download={att.filename ?? "attachment"}
                  className="ml-1"
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

      <div className="flex items-center gap-1 border-t px-4 py-2">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1 px-2"
          onClick={() =>
            mailAction.mutate({
              mailId: mail.id,
              accountId: mail.account_id,
              action: { action: mail.is_seen ? "mark_unread" : "mark_read" },
            })
          }
          title={mail.is_seen ? "Mark as unread" : "Mark as read"}
          aria-label={mail.is_seen ? "Mark as unread" : "Mark as read"}
        >
          {mail.is_seen ? (
            <MailIcon className="h-3.5 w-3.5" />
          ) : (
            <MailOpen className="h-3.5 w-3.5" />
          )}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1 px-2"
          onClick={() =>
            mailAction.mutate({
              mailId: mail.id,
              accountId: mail.account_id,
              action: { action: mail.is_flagged ? "unflag" : "flag" },
            })
          }
          title={mail.is_flagged ? "Unflag" : "Star"}
          aria-label={mail.is_flagged ? "Unflag" : "Star"}
        >
          <Star
            className={
              mail.is_flagged
                ? "h-3.5 w-3.5 fill-yellow-400 text-yellow-400"
                : "h-3.5 w-3.5"
            }
          />
        </Button>
        <a
          href={api.mails.rawUrl(mail.id)}
          download={`${mail.subject ?? "message"}.eml`}
          className="flex h-7 items-center gap-1 rounded-md px-2 text-muted-foreground hover:bg-muted hover:text-foreground"
          title="Download as .eml"
          aria-label="Download as .eml"
        >
          <FileDown className="h-3.5 w-3.5" />
        </a>
        {mail.verdict?.is_spam && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2"
            onClick={() => {
              // Records the correction for the classifier and moves the
              // message out of Junk -- two distinct writes, both needed.
              verdictFeedback.mutate({
                mailId: mail.id,
                accountId: mail.account_id,
                isSpam: false,
              });
              mailAction.mutate({
                mailId: mail.id,
                accountId: mail.account_id,
                action: { action: "not_spam" },
              });
            }}
            title="Mark as not spam"
            aria-label="Mark as not spam"
          >
            <ThumbsUp className="h-3.5 w-3.5" />
          </Button>
        )}
        {mail.verdict && !mail.verdict.is_spam && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2"
            onClick={() =>
              verdictFeedback.mutate({
                mailId: mail.id,
                accountId: mail.account_id,
                isSpam: true,
              })
            }
            title="Mark as spam"
            aria-label="Mark as spam"
          >
            <ThumbsDown className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    </div>
  );
}
