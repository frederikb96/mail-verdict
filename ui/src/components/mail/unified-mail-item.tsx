"use client";

/**
 * Mail list item variant for unified view.
 *
 * Same as MailListItem but with an emoji badge identifying the source account.
 */

import { Star, Archive, Ban, Trash2, MailOpen, Mail as MailIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  extractSenderName,
  formatRelativeDate,
  getInitials,
} from "@/lib/format";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Checkbox } from "@/components/ui/checkbox";
import type { UnifiedMailSummary } from "@/types/api";

interface UnifiedMailItemProps {
  mail: UnifiedMailSummary;
  isSelected: boolean;
  isFocused?: boolean;
  isChecked: boolean;
  selectionMode: boolean;
  onSelect: (mailId: string) => void;
  onCheckToggle: (mailId: string, shiftKey: boolean) => void;
  onAction?: (
    mailId: string,
    action: "flag" | "unflag" | "archive" | "spam" | "delete" | "mark_read" | "mark_unread",
    mailAccountId?: string,
  ) => void;
}

export function UnifiedMailItem({
  mail,
  isSelected,
  isFocused,
  isChecked,
  selectionMode,
  onSelect,
  onCheckToggle,
  onAction,
}: UnifiedMailItemProps) {
  const senderName = extractSenderName(mail.from_addr);
  const initials = getInitials(senderName);

  return (
    <div
      className={cn(
        "group flex cursor-pointer items-start gap-3 border-b px-4 py-3 transition-colors",
        isSelected
          ? "bg-accent border-l-2 border-l-primary"
          : isChecked
            ? "bg-accent/70"
            : "hover:bg-accent/50",
        !mail.is_read && !isSelected && !isChecked && "bg-accent/20",
        isFocused && "ring-2 ring-inset ring-ring",
      )}
      onClick={() => onSelect(mail.id)}
    >
      {/* Checkbox (visible in selection mode or on hover) */}
      <div
        className={cn(
          "shrink-0",
          selectionMode ? "block" : "hidden group-hover:block",
        )}
      >
        <Checkbox
          checked={isChecked}
          onCheckedChange={() => {}}
          onClick={(e) => {
            e.stopPropagation();
            onCheckToggle(mail.id, e.shiftKey);
          }}
          className="h-4 w-4"
        />
      </div>

      {/* Avatar with emoji badge (hidden when checkbox visible) */}
      <div
        className={cn(
          "relative shrink-0",
          selectionMode && "hidden",
          !selectionMode && "group-hover:hidden",
        )}
      >
        <Avatar className="h-8 w-8">
          <AvatarFallback className="text-xs">{initials}</AvatarFallback>
        </Avatar>
        {mail.account_emoji && (
          <span
            className="absolute -bottom-1 -right-1 text-xs leading-none"
            title="Source account"
          >
            {mail.account_emoji}
          </span>
        )}
      </div>

      {/* Content */}
      <div className="flex min-w-0 flex-1 flex-col justify-center overflow-hidden">
        <div className="flex items-center gap-2">
          {/* Emoji badge inline (always visible, small) */}
          {mail.account_emoji && (
            <span className="shrink-0 text-xs" title="Source account">
              {mail.account_emoji}
            </span>
          )}
          {/* Unread dot */}
          {!mail.is_read && (
            <div className="h-2 w-2 shrink-0 rounded-full bg-blue-500" />
          )}
          <span
            className={cn(
              "truncate text-sm text-foreground",
              !mail.is_read ? "font-semibold" : "font-medium",
            )}
          >
            {senderName}
          </span>
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">
            {formatRelativeDate(mail.received_at)}
          </span>
        </div>
        <div className="truncate text-sm text-muted-foreground">
          {mail.subject ?? "(no subject)"}
        </div>
        {mail.snippet && (
          <div className="line-clamp-1 text-xs text-muted-foreground">
            {mail.snippet}
          </div>
        )}
      </div>

      {/* Always-visible indicators */}
      <div className="flex shrink-0 items-center gap-1">
        <button
          className="rounded-md p-1 text-muted-foreground hover:text-foreground transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, mail.is_read ? "mark_unread" : "mark_read", mail.account_id);
          }}
          title={mail.is_read ? "Mark as unread" : "Mark as read"}
        >
          {mail.is_read ? (
            <MailIcon className="h-3.5 w-3.5" />
          ) : (
            <MailOpen className="h-3.5 w-3.5" />
          )}
        </button>

        {mail.is_flagged && (
          <Star className="h-4 w-4 fill-yellow-400 text-yellow-400 group-hover:hidden" />
        )}
      </div>

      {/* Hover actions */}
      <div className="hidden shrink-0 items-center gap-1 group-hover:flex">
        <button
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(
              mail.id,
              mail.is_flagged ? "unflag" : "flag",
              mail.account_id,
            );
          }}
          title={mail.is_flagged ? "Unflag" : "Star"}
        >
          <Star
            className={cn(
              "h-4 w-4",
              mail.is_flagged
                ? "fill-yellow-400 text-yellow-400"
                : "text-muted-foreground",
            )}
          />
        </button>
        <button
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, "archive", mail.account_id);
          }}
          title="Archive"
        >
          <Archive className="h-4 w-4 text-muted-foreground" />
        </button>
        <button
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, "spam", mail.account_id);
          }}
          title="Spam"
        >
          <Ban className="h-4 w-4 text-muted-foreground" />
        </button>
        <button
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, "delete", mail.account_id);
          }}
          title="Delete"
        >
          <Trash2 className="h-4 w-4 text-muted-foreground" />
        </button>
      </div>
    </div>
  );
}
