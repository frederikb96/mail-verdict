"use client";

import { useEffect, useRef, useState } from "react";
import { useAtomValue, useSetAtom } from "jotai";
import {
  Mail,
  Paperclip,
  Download,
  ShieldAlert,
  Trash2,
  Star,
  Archive,
  Ban,
  MailOpen,
  MailIcon,
  ChevronRight,
  ChevronDown,
  Loader2,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { EmailRenderer } from "@/components/mail/email-renderer";
import { ImageBanner } from "@/components/mail/image-banner";
import { TruncatedBanner } from "@/components/mail/truncated-banner";
import { ReplyBox } from "@/components/mail/reply-box";
import { DraftEditor } from "@/components/mail/draft-editor";
import { api } from "@/lib/api";
import { useMailAction, useThread } from "@/hooks/use-mails";
import { useAccount } from "@/hooks/use-accounts";
import { useVerdictFeedback } from "@/hooks/use-verdicts";
import { selectedMailIdAtom } from "@/lib/atoms";
import {
  extractSenderName,
  extractEmail,
  formatAddresses,
  formatFullDate,
  formatRelativeDate,
  formatSize,
} from "@/lib/format";
import type { MessageDetail } from "@/types/api";

function ThreadMessage({
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
        onClick={onToggle}
        className="flex w-full items-center gap-3 border-b px-4 py-2.5 text-left hover:bg-accent/50"
      >
        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
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
          onClick={onToggle}
          className="flex items-start justify-between gap-4 text-left"
        >
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

      {mail.is_truncated && <TruncatedBanner />}

      {!mail.is_truncated && (
        <ImageBanner
          accountId={mail.account_id}
          senderEmail={senderEmail}
          senderDomain={senderEmail?.split("@")[1] ?? null}
          imagesAllowed={mail.images_allowed}
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
        >
          <Star
            className={
              mail.is_flagged
                ? "h-3.5 w-3.5 fill-yellow-400 text-yellow-400"
                : "h-3.5 w-3.5"
            }
          />
        </Button>
        {mail.verdict?.is_spam && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2"
            onClick={() =>
              verdictFeedback.mutate({
                mailId: mail.id,
                accountId: mail.account_id,
                isSpam: false,
              })
            }
            title="Mark as not spam"
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
          >
            <ThumbsDown className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    </div>
  );
}

export function ReadingPane() {
  const mailId = useAtomValue(selectedMailIdAtom);
  const setSelectedMailId = useSetAtom(selectedMailIdAtom);
  const { data: thread, isLoading } = useThread(mailId);
  const mailAction = useMailAction();
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [imageOverrides, setImageOverrides] = useState<Set<string>>(new Set());
  const autoReadRef = useRef<string | null>(null);

  const messages = thread?.messages ?? [];
  const primary =
    messages.find((m) => m.id === mailId) ?? messages[messages.length - 1] ?? null;
  const account = useAccount(primary?.account_id ?? null);
  const isDraft = primary?.is_draft ?? false;

  // Reset expansion state per opened mail: last message expanded, plus
  // whichever message the user actually clicked in the list.
  useEffect(() => {
    if (messages.length === 0) return;
    const next = new Set<string>();
    const last = messages[messages.length - 1];
    next.add(last.id);
    if (mailId) next.add(mailId);
    setExpandedIds(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mailId, messages.length > 0 ? messages[0].id : null]);

  // Auto mark-as-read the specific message the user opened -- skipped for a
  // draft, which is about to be edited or replaced rather than read.
  useEffect(() => {
    if (primary && !isDraft && !primary.is_seen && primary.id !== autoReadRef.current) {
      autoReadRef.current = primary.id;
      mailAction.mutate({
        mailId: primary.id,
        accountId: primary.account_id,
        action: { action: "mark_read" },
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [primary?.id, primary?.is_seen, isDraft]);

  const toggle = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (!mailId) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <Mail className="h-16 w-16 opacity-30" />
        <p className="text-sm">Select a message to read</p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex flex-col gap-4 p-6">
        <Skeleton className="h-8 w-3/4" />
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-4 w-1/3" />
        <Separator />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (!primary) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <ShieldAlert className="h-12 w-12 opacity-50" />
        <p className="text-sm">Message not found</p>
      </div>
    );
  }

  if (isDraft) {
    return <DraftEditor mail={primary} onDone={() => setSelectedMailId(null)} />;
  }

  return (
    <div className="flex h-full flex-col">
      {/* Subject and top-level actions apply to the message the user opened */}
      <div className="flex items-start justify-between gap-4 border-b p-4">
        <h2 className="text-lg font-semibold leading-tight">
          {primary.subject ?? "(no subject)"}
        </h2>
        <div className="flex shrink-0 items-center gap-1">
          {messages.length > 1 && (
            <Badge variant="secondary" className="mr-1">
              {messages.length} messages
            </Badge>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() =>
              mailAction.mutate({
                mailId: primary.id,
                accountId: primary.account_id,
                action: { action: "archive" },
              })
            }
            title="Archive"
          >
            <Archive className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() =>
              mailAction.mutate({
                mailId: primary.id,
                accountId: primary.account_id,
                action: { action: "spam" },
              })
            }
            title="Spam"
          >
            <Ban className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() =>
              mailAction.mutate({
                mailId: primary.id,
                accountId: primary.account_id,
                action: { action: "trash" },
              })
            }
            title="Move to trash"
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {messages.map((m) => (
          <ThreadMessage
            key={m.id}
            mail={m}
            expanded={expandedIds.has(m.id)}
            onToggle={() => toggle(m.id)}
            imagesAllowedOverride={imageOverrides.has(m.id)}
            onLoadImages={() =>
              setImageOverrides((prev) => new Set(prev).add(m.id))
            }
          />
        ))}
      </div>

      {messages.length > 0 && (
        <ReplyBox
          source={messages[messages.length - 1]}
          ownEmail={account.data?.imap_user ?? ""}
        />
      )}
    </div>
  );
}
