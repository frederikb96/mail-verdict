"use client";

import { Star, Archive, Ban, ThumbsUp, Trash2, MailOpen, Mail as MailIcon, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { extractSenderName, formatRelativeDate } from "@/lib/format";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import type { MessageActionType, MessageSummary } from "@/types/api";

type RowAction = Extract<
  MessageActionType,
  | "flag"
  | "unflag"
  | "archive"
  | "spam"
  | "not_spam"
  | "trash"
  | "mark_read"
  | "mark_unread"
>;

// Kept in the layout at full width at all times; only opacity/pointer-events
// toggle, so revealing these controls never shifts anything else in the row.
const revealOnHoverClass =
  "opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto group-focus-within:opacity-100 group-focus-within:pointer-events-auto";

interface MailListItemProps {
  mail: MessageSummary;
  isSelected: boolean;
  isFocused?: boolean;
  isChecked: boolean;
  selectionMode: boolean;
  /** True when the row's folder is Junk -- swaps the Spam control for Not spam. */
  isJunk?: boolean;
  onSelect: (mailId: string) => void;
  onCheckToggle: (mailId: string, shiftKey: boolean) => void;
  onAction?: (mailId: string, action: RowAction) => void;
}

export function MailListItem({
  mail,
  isSelected,
  isFocused,
  isChecked,
  selectionMode,
  isJunk,
  onSelect,
  onCheckToggle,
  onAction,
}: MailListItemProps) {
  const senderName = extractSenderName(mail.from_addr);

  return (
    <div
      className={cn(
        "group flex cursor-pointer items-start gap-3 border-b px-4 py-3 transition-colors",
        isSelected
          ? "bg-accent border-l-2 border-l-primary"
          : isChecked
            ? "bg-accent/70"
            : "hover:bg-accent/50",
        !mail.is_seen && !isSelected && !isChecked && "bg-accent/20",
        isFocused && "ring-2 ring-inset ring-ring",
        mail.pending_sync && "opacity-60",
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

      {/* Avatar (hidden when checkbox visible in selection mode) */}
      <InitialsAvatar
        name={senderName}
        className={cn(selectionMode && "hidden", !selectionMode && "group-hover:hidden")}
      />

      {/* Content */}
      <div className="flex min-w-0 flex-1 flex-col justify-center overflow-hidden">
        <div className="flex items-center gap-2">
          {/* Unread dot */}
          {!mail.is_seen && (
            <div className="h-2 w-2 shrink-0 rounded-full bg-blue-500" />
          )}
          <span
            className={cn(
              "truncate text-sm text-foreground",
              !mail.is_seen ? "font-semibold" : "font-medium",
            )}
          >
            {senderName}
          </span>
          {mail.pending_sync && (
            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />
          )}
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">
            {formatRelativeDate(mail.received_at)}
          </span>
        </div>
        <div className="flex items-center gap-1.5 truncate text-sm text-muted-foreground">
          <span className="truncate">{mail.subject ?? "(no subject)"}</span>
          {mail.thread_count && mail.thread_count > 1 && (
            <Badge variant="secondary" className="h-4 shrink-0 px-1 text-[10px]">
              {mail.thread_count}
            </Badge>
          )}
        </div>
        {mail.snippet && (
          <div className="line-clamp-1 text-xs text-muted-foreground">
            {mail.snippet}
          </div>
        )}
      </div>

      {/*
        Row actions. Every button is always in the DOM at a fixed position -
        only opacity/pointer-events toggle on hover or focus - so the
        always-visible mark-read control never shifts under the pointer when
        the rest of the row reveals itself.
      */}
      <div className="flex shrink-0 items-center gap-1">
        <button
          className={cn(
            "rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
            !mail.is_flagged && revealOnHoverClass,
          )}
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, mail.is_flagged ? "unflag" : "flag");
          }}
          title={mail.is_flagged ? "Unflag" : "Star"}
          aria-label={mail.is_flagged ? "Unflag" : "Star"}
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
          className={cn(
            "rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
            revealOnHoverClass,
          )}
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, "archive");
          }}
          title="Archive"
          aria-label="Archive"
        >
          <Archive className="h-4 w-4 text-muted-foreground" />
        </button>
        {isJunk ? (
          <button
            className={cn(
              "rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
              revealOnHoverClass,
            )}
            onClick={(e) => {
              e.stopPropagation();
              onAction?.(mail.id, "not_spam");
            }}
            title="Not spam"
            aria-label="Not spam"
          >
            <ThumbsUp className="h-4 w-4 text-muted-foreground" />
          </button>
        ) : (
          <button
            className={cn(
              "rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
              revealOnHoverClass,
            )}
            onClick={(e) => {
              e.stopPropagation();
              onAction?.(mail.id, "spam");
            }}
            title="Spam"
            aria-label="Mark as spam"
          >
            <Ban className="h-4 w-4 text-muted-foreground" />
          </button>
        )}
        <button
          className={cn(
            "rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
            revealOnHoverClass,
          )}
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, "trash");
          }}
          title="Move to trash"
          aria-label="Move to trash"
        >
          <Trash2 className="h-4 w-4 text-muted-foreground" />
        </button>
        <button
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, mail.is_seen ? "mark_unread" : "mark_read");
          }}
          title={mail.is_seen ? "Mark as unread" : "Mark as read"}
          aria-label={mail.is_seen ? "Mark as unread" : "Mark as read"}
        >
          {mail.is_seen ? (
            <MailIcon className="h-4 w-4" />
          ) : (
            <MailOpen className="h-4 w-4" />
          )}
        </button>
      </div>
    </div>
  );
}
