"use client";

import { useEffect, useRef, useState } from "react";
import { useAtomValue } from "jotai";
import {
  Mail,
  Paperclip,
  Download,
  Shield,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Trash2,
  Star,
  MailOpen,
  MailIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { EmailRenderer } from "@/components/mail/email-renderer";
import { ImageBanner } from "@/components/mail/image-banner";
import { useMailDetail, useMailAction } from "@/hooks/use-mails";
import { selectedAccountIdAtom, selectedMailIdAtom } from "@/lib/atoms";
import {
  extractSenderName,
  extractEmail,
  formatFullDate,
  formatSize,
  formatAddresses,
} from "@/lib/format";

function AuthBadge({
  label,
  pass,
}: {
  label: string;
  pass: boolean | null;
}) {
  if (pass === null) return null;
  return (
    <Badge
      variant={pass ? "default" : "destructive"}
      className="gap-1 text-xs"
    >
      {pass ? (
        <ShieldCheck className="h-3 w-3" />
      ) : (
        <ShieldX className="h-3 w-3" />
      )}
      {label}
    </Badge>
  );
}

export function ReadingPane() {
  const mailId = useAtomValue(selectedMailIdAtom);
  const accountId = useAtomValue(selectedAccountIdAtom);
  const { data: mail, isLoading } = useMailDetail(mailId, accountId);
  const mailAction = useMailAction();
  const [loadImagesForMessage, setLoadImagesForMessage] = useState(false);

  // Auto mark-as-read when a mail is displayed
  const autoReadRef = useRef<string | null>(null);
  useEffect(() => {
    if (mail && !mail.is_read && mail.id !== autoReadRef.current) {
      autoReadRef.current = mail.id;
      mailAction.mutate({
        mailId: mail.id,
        accountId: mail.account_id,
        action: { action: "mark_read" },
      });
    }
  }, [mail?.id, mail?.is_read]);

  // Empty state
  if (!mailId) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <Mail className="h-16 w-16 opacity-30" />
        <p className="text-sm">Select a message to read</p>
      </div>
    );
  }

  // Loading skeleton
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

  if (!mail) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <ShieldAlert className="h-12 w-12 opacity-50" />
        <p className="text-sm">Message not found</p>
      </div>
    );
  }

  const senderName = extractSenderName(mail.from_addr);
  const senderEmail = extractEmail(mail.from_addr);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex flex-col gap-3 border-b p-4">
        {/* Subject and actions */}
        <div className="flex items-start justify-between gap-4">
          <h2 className="text-lg font-semibold leading-tight">
            {mail.subject ?? "(no subject)"}
          </h2>
          <div className="flex shrink-0 items-center gap-1">
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() =>
                mailAction.mutate({
                  mailId: mail.id,
                  accountId: mail.account_id,
                  action: {
                    action: mail.is_read ? "mark_unread" : "mark_read",
                  },
                })
              }
              title={mail.is_read ? "Mark as unread" : "Mark as read"}
            >
              {mail.is_read ? (
                <MailIcon className="h-4 w-4" />
              ) : (
                <MailOpen className="h-4 w-4" />
              )}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() =>
                mailAction.mutate({
                  mailId: mail.id,
                  accountId: mail.account_id,
                  action: {
                    action: mail.is_flagged ? "unflag" : "flag",
                  },
                })
              }
              title={mail.is_flagged ? "Unflag" : "Flag"}
            >
              <Star
                className={
                  mail.is_flagged
                    ? "h-4 w-4 fill-yellow-400 text-yellow-400"
                    : "h-4 w-4"
                }
              />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() =>
                mailAction.mutate({
                  mailId: mail.id,
                  accountId: mail.account_id,
                  action: { action: "delete" },
                })
              }
              title="Delete"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Sender info */}
        <div className="flex flex-col gap-1 text-sm">
          <div className="flex items-center gap-2">
            <span className="font-medium">{senderName}</span>
            <span className="text-muted-foreground">&lt;{senderEmail}&gt;</span>
          </div>
          <div className="text-muted-foreground">
            To: {formatAddresses(mail.to_addrs)}
          </div>
          {mail.cc_addrs && (
            <div className="text-muted-foreground">
              Cc: {formatAddresses(mail.cc_addrs)}
            </div>
          )}
          <div className="text-xs text-muted-foreground">
            {formatFullDate(mail.received_at)}
          </div>
        </div>

        {/* Auth badges */}
        <div className="flex flex-wrap gap-1.5">
          <AuthBadge label="DKIM" pass={mail.dkim_pass} />
          <AuthBadge label="SPF" pass={mail.spf_pass} />
          <AuthBadge label="DMARC" pass={mail.dmarc_pass} />
          {mail.tags.map((tag) => (
            <Badge key={tag.tag_name} variant="outline" className="text-xs">
              {tag.tag_name}
            </Badge>
          ))}
        </div>

        {/* Body sync indicator */}
        {!mail.body_synced && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <div className="h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
            Loading message body...
          </div>
        )}
      </div>

      {/* Image blocking banner */}
      <ImageBanner
        accountId={mail.account_id}
        senderEmail={senderEmail}
        senderDomain={senderEmail?.split("@")[1] ?? null}
        imagesAllowed={mail.images_allowed}
        hasBlockedImages={mail.has_blocked_images}
        onLoadForMessage={() => setLoadImagesForMessage(true)}
      />

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-auto">
        <EmailRenderer
          html={mail.body_html}
          plainText={mail.body_text}
          imagesAllowed={mail.images_allowed || loadImagesForMessage}
        />
      </div>

      {/* Attachments */}
      {mail.attachments.length > 0 && (
        <div className="border-t p-4">
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
                  href={`/api/mails/${mail.id}/attachments/${att.id}`}
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
    </div>
  );
}
