"use client";

/**
 * Mail list item variant for unified view.
 *
 * Same as MailListItem but with an emoji badge identifying the source account.
 */

import { Star, Archive, Ban, Trash2, MailOpen, Mail as MailIcon, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { extractSenderName, formatRelativeDate } from "@/lib/format";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import type { MessageActionType, UnifiedMessageSummary } from "@/types/api";

type RowAction = Extract<
  MessageActionType,
  "flag" | "unflag" | "archive" | "spam" | "trash" | "mark_read" | "mark_unread"
>;

// Opacity/pointer-events only -- these float over the row's own background
// rather than reserving layout space, so the sender/subject/snippet keep
// the row's full width whether or not the pointer is anywhere near it.
const revealOnHoverClass =
  "opacity-0 pointer-events-none group-hover/row:opacity-100 group-hover/row:pointer-events-auto group-focus-within/row:opacity-100 group-focus-within/row:pointer-events-auto";

interface UnifiedMailItemProps {
  mail: UnifiedMessageSummary;
  isSelected: boolean;
  isFocused?: boolean;
  isChecked: boolean;
  selectionMode: boolean;
  onOpen: (mailId: string) => void;
  onCheckToggle: (mailId: string, shiftKey: boolean) => void;
  onAction?: (mailId: string, action: RowAction, mailAccountId?: string) => void;
}

export function UnifiedMailItem({
  mail,
  isSelected,
  isFocused,
  isChecked,
  selectionMode,
  onOpen,
  onCheckToggle,
  onAction,
}: UnifiedMailItemProps) {
  const senderName = extractSenderName(mail.from_addr);

  const handleRowClick = (e: React.MouseEvent) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey) {
      onCheckToggle(mail.id, e.shiftKey);
      return;
    }
    onOpen(mail.id);
  };

  return (
    <div
      className={cn(
        // No "group"/tabIndex here -- DragMail's own wrapper is the row's
        // one real tab stop (dnd-kit's keyboard-drag support already
        // needs it) and declares the named group every hover/focus
        // reveal below keys off instead.
        "relative flex cursor-pointer items-start gap-3 border-b px-4 py-3 transition-colors",
        isSelected
          ? "bg-accent border-l-2 border-l-primary"
          : isChecked
            ? "bg-accent/70"
            : "hover:bg-accent/50",
        !mail.is_seen && !isSelected && !isChecked && "bg-accent/20",
        isFocused && "ring-2 ring-inset ring-ring",
        mail.pending_sync && "opacity-60",
      )}
      onClick={handleRowClick}
    >
      {/* Avatar/checkbox slot with the emoji badge -- the checkbox is how selection starts. */}
      <div className="relative h-8 w-8 shrink-0">
        <InitialsAvatar
          name={senderName}
          className={cn(
            "absolute inset-0",
            selectionMode
              ? "hidden"
              : "opacity-100 transition-opacity group-hover/row:opacity-0 group-focus-within/row:opacity-0",
          )}
          badge={
            mail.account_emoji && <span title="Source account">{mail.account_emoji}</span>
          }
        />
        <div
          className={cn(
            "absolute inset-0 flex items-center justify-center",
            selectionMode
              ? "opacity-100"
              : "opacity-0 pointer-events-none transition-opacity group-hover/row:opacity-100 group-hover/row:pointer-events-auto group-focus-within/row:opacity-100 group-focus-within/row:pointer-events-auto",
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
      </div>

      {/* Content -- always the row's full width; controls float over it. */}
      <div className="flex min-w-0 flex-1 flex-col justify-center overflow-hidden">
        <div className="flex items-center gap-2">
          {/* Emoji badge inline (always visible, small) */}
          {mail.account_emoji && (
            <span className="shrink-0 text-xs" title="Source account">
              {mail.account_emoji}
            </span>
          )}
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
        Floating controls: positioned over the row rather than reserved in
        its flex layout, vertically centered so they never cover the
        timestamp sitting at the very top of the row. Every button stays
        in the DOM at all times -- only opacity/pointer-events toggle on
        hover or focus -- so nothing shifts under the pointer as the row
        reveals itself.
      */}
      <div className="pointer-events-none absolute inset-y-0 right-2 flex items-center gap-0.5">
        <button
          className={cn(
            "pointer-events-auto rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
            !mail.is_flagged && revealOnHoverClass,
          )}
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, mail.is_flagged ? "unflag" : "flag", mail.account_id);
          }}
          title={mail.is_flagged ? "Unstar" : "Star"}
          aria-label={mail.is_flagged ? "Unstar" : "Star"}
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
            "pointer-events-auto rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
            revealOnHoverClass,
          )}
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, "archive", mail.account_id);
          }}
          title="Archive"
          aria-label="Archive"
        >
          <Archive className="h-4 w-4 text-muted-foreground" />
        </button>
        <button
          className={cn(
            "pointer-events-auto rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
            revealOnHoverClass,
          )}
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, "spam", mail.account_id);
          }}
          title="Move to Junk"
          aria-label="Move to Junk"
        >
          <Ban className="h-4 w-4 text-muted-foreground" />
        </button>
        <button
          className="pointer-events-auto rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, mail.is_seen ? "mark_unread" : "mark_read", mail.account_id);
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

      {/* Delete sits apart from the rest, lower right, so a reach for
          Archive or Junk doesn't land on it by mistake. */}
      <button
        className={cn(
          "absolute bottom-1.5 right-2 rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-destructive transition-colors",
          revealOnHoverClass,
        )}
        onClick={(e) => {
          e.stopPropagation();
          onAction?.(mail.id, "trash", mail.account_id);
        }}
        title="Move to trash"
        aria-label="Move to trash"
      >
        <Trash2 className="h-4 w-4 text-muted-foreground" />
      </button>
    </div>
  );
}
