"use client";

import { Star, Archive, Ban, ThumbsUp, Trash2, MailOpen, Mail as MailIcon, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { extractEmail, extractSenderName, formatRelativeDate } from "@/lib/format";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { useContactPhotoIndex } from "@/hooks/use-contacts";
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

// Opacity/pointer-events only -- these float over the row's own background
// rather than reserving layout space, so the sender/subject/snippet keep
// the row's full width whether or not the pointer is anywhere near it.
const revealOnHoverClass =
  "opacity-0 pointer-events-none group-hover/row:opacity-100 group-hover/row:pointer-events-auto group-focus-within/row:opacity-100 group-focus-within/row:pointer-events-auto";

interface MailListItemProps {
  mail: MessageSummary;
  isSelected: boolean;
  isFocused?: boolean;
  isChecked: boolean;
  selectionMode: boolean;
  /** True when the row's folder is Junk -- swaps the Junk control for Remove from Junk. */
  isJunk?: boolean;
  /** A row action here is scoped to the thread's latest message only --
   * the tooltip says so rather than leaving it ambiguous. */
  isThreaded?: boolean;
  onOpen: (mailId: string) => void;
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
  isThreaded,
  onOpen,
  onCheckToggle,
  onAction,
}: MailListItemProps) {
  const senderName = extractSenderName(mail.from_addr);
  const threadSuffix = isThreaded ? " (latest message in thread)" : "";

  // One request per account rendered (deduped/cached by TanStack Query
  // across every row sharing it), never one per row -- see
  // useContactPhotoIndex.
  const { data: photoIndex } = useContactPhotoIndex(mail.account_id);
  const senderEmail = extractEmail(mail.from_addr).toLowerCase();
  const photoUrl = photoIndex?.by_email[senderEmail]?.photo_url ?? null;

  const handleRowClick = (e: React.MouseEvent) => {
    // ctrl/cmd+click and shift+click on the row's own text are selection
    // gestures, not "open" -- routed through the same toggle the checkbox
    // uses, which already understands shiftKey as a range extension.
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
      {/* Avatar/checkbox slot -- the checkbox is how selection starts. */}
      <div className="relative h-8 w-8 shrink-0">
        <InitialsAvatar
          name={senderName}
          photoUrl={photoUrl}
          className={cn(
            "absolute inset-0",
            selectionMode
              ? "hidden"
              : "opacity-100 transition-opacity group-hover/row:opacity-0 group-focus-within/row:opacity-0",
          )}
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
            onAction?.(mail.id, mail.is_flagged ? "unflag" : "flag");
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
            onAction?.(mail.id, "archive");
          }}
          title={`Archive${threadSuffix}`}
          aria-label={`Archive${threadSuffix}`}
        >
          <Archive className="h-4 w-4 text-muted-foreground" />
        </button>
        {isJunk ? (
          <button
            className={cn(
              "pointer-events-auto rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
              revealOnHoverClass,
            )}
            onClick={(e) => {
              e.stopPropagation();
              onAction?.(mail.id, "not_spam");
            }}
            title={`Remove from Junk${threadSuffix}`}
            aria-label={`Remove from Junk${threadSuffix}`}
          >
            <ThumbsUp className="h-4 w-4 text-muted-foreground" />
          </button>
        ) : (
          <button
            className={cn(
              "pointer-events-auto rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
              revealOnHoverClass,
            )}
            onClick={(e) => {
              e.stopPropagation();
              onAction?.(mail.id, "spam");
            }}
            title={`Move to Junk${threadSuffix}`}
            aria-label={`Move to Junk${threadSuffix}`}
          >
            <Ban className="h-4 w-4 text-muted-foreground" />
          </button>
        )}
        <button
          className="pointer-events-auto rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
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

      {/* Delete sits apart from the rest, lower right, so a reach for
          Archive or Junk doesn't land on it by mistake. */}
      <button
        className={cn(
          "absolute bottom-1.5 right-2 rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-destructive transition-colors",
          revealOnHoverClass,
        )}
        onClick={(e) => {
          e.stopPropagation();
          onAction?.(mail.id, "trash");
        }}
        title={`Move to trash${threadSuffix}`}
        aria-label={`Move to trash${threadSuffix}`}
      >
        <Trash2 className="h-4 w-4 text-muted-foreground" />
      </button>
    </div>
  );
}
