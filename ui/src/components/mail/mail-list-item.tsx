"use client";

import { Star, Archive, Ban, Undo2, Trash2, MailOpen, Mail as MailIcon, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { extractEmail, extractSenderName, formatRelativeDate } from "@/lib/format";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { useContactPhotoIndex } from "@/hooks/use-contacts";
import type { MailRowAction, MessageSummary } from "@/types/api";

// Opacity/pointer-events only -- these float over the row's own background
// rather than reserving layout space, so the sender/subject/snippet keep
// the row's full width whether or not the pointer is anywhere near it.
const revealOnHoverClass =
  "opacity-0 pointer-events-none group-hover/row:opacity-100 group-hover/row:pointer-events-auto group-focus-within/row:opacity-100 group-focus-within/row:pointer-events-auto";

interface MailListItemProps {
  mail: MessageSummary;
  /** The row's account, as the avatar's badge -- given only where rows from
   * several accounts share one list (a unified view). The avatar is the
   * one place it is shown; the sender line never repeats it. */
  accountEmoji?: string | null;
  accountName?: string;
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
  onAction?: (mailId: string, action: MailRowAction, mailAccountId?: string) => void;
}

/**
 * One row of any mail list -- a folder's or a unified view's.
 *
 * Unread is carried by four things at once, so it survives a glance in
 * either theme: a dot in a gutter column of its own (every sender lines up
 * whatever the state), a tinted row, a bold sender and a full-contrast
 * subject. A read row is quieter throughout. The subject is bold on every
 * row, a step smaller than the sender, so weight alone never has to tell
 * the two states apart.
 */
export function MailListItem({
  mail,
  accountEmoji,
  accountName,
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
  const unread = !mail.is_seen;

  // One request per account rendered (deduped/cached by TanStack Query
  // across every row sharing it), never one per row -- see
  // useContactPhotoIndex.
  const { data: photoIndex } = useContactPhotoIndex(mail.account_id);
  const senderEmail = extractEmail(mail.from_addr).toLowerCase();
  const photoUrl = photoIndex?.by_email[senderEmail]?.photo_url ?? null;

  const act = (action: MailRowAction) => onAction?.(mail.id, action, mail.account_id);

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
      data-unread={unread ? "true" : "false"}
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
            : unread
              ? "bg-sky-500/[0.07] hover:bg-sky-500/[0.12] dark:bg-sky-400/[0.08] dark:hover:bg-sky-400/[0.13]"
              : "hover:bg-accent/50",
        isFocused && "ring-2 ring-inset ring-ring",
        mail.pending_sync && "opacity-60",
      )}
      onClick={handleRowClick}
    >
      {/* The unread marker's own column: the row's left padding, centred on
          the sender line. */}
      {unread && (
        <span
          data-testid="unread-dot"
          className="absolute left-1.5 top-[18px] h-2 w-2 rounded-full bg-sky-500 dark:bg-sky-400"
        >
          <span className="sr-only">Unread</span>
        </span>
      )}

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
          badge={
            accountEmoji && (
              <span data-testid="account-badge" title={accountName}>
                {accountEmoji}
              </span>
            )
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

      {/* Content -- always the row's full width; the hover-only controls
          below float over it. Star (once flagged) is the one control a row
          shows *persistently*, so it lives here in a real, reserved slot
          next to the timestamp, beside the read/unread toggle that only
          appears on hover -- never the row's vertical centre line, which
          for a two-line row (no snippet) sits across the very text above. */}
      <div className="flex min-w-0 flex-1 flex-col justify-center overflow-hidden">
        <div data-slot="row-sender-line" className="flex items-center gap-2">
          <span
            data-slot="row-sender"
            className={cn(
              "truncate text-sm",
              unread ? "font-bold text-foreground" : "font-normal text-foreground/75",
            )}
          >
            {senderName}
          </span>
          {mail.pending_sync && (
            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />
          )}
          <div className="ml-auto flex shrink-0 items-center gap-0.5">
            <button
              className={cn(
                "rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
                !mail.is_flagged && revealOnHoverClass,
              )}
              onClick={(e) => {
                e.stopPropagation();
                act(mail.is_flagged ? "unflag" : "flag");
              }}
              title={mail.is_flagged ? "Unstar" : "Star"}
              aria-label={mail.is_flagged ? "Unstar" : "Star"}
            >
              <Star
                className={cn(
                  "h-3.5 w-3.5",
                  mail.is_flagged
                    ? "fill-yellow-400 text-yellow-400"
                    : "text-muted-foreground",
                )}
              />
            </button>
            {/* Revealed on hover like every other row action: the dot
                already says whether the row is unread, and a permanent
                envelope showing the *opposite* state (the action it would
                take) read as a second, contradictory state marker. */}
            <button
              className={cn(
                "rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
                revealOnHoverClass,
              )}
              onClick={(e) => {
                e.stopPropagation();
                act(mail.is_seen ? "mark_unread" : "mark_read");
              }}
              title={mail.is_seen ? "Mark as unread" : "Mark as read"}
              aria-label={mail.is_seen ? "Mark as unread" : "Mark as read"}
            >
              {mail.is_seen ? (
                <MailIcon className="h-3.5 w-3.5" />
              ) : (
                <MailOpen className="h-3.5 w-3.5" />
              )}
            </button>
            <span
              data-slot="row-date"
              className={cn(
                "text-xs",
                unread ? "font-semibold text-foreground" : "text-muted-foreground",
              )}
            >
              {formatRelativeDate(mail.received_at)}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-1.5 truncate">
          <span
            data-slot="row-subject"
            className={cn(
              "truncate text-[13px] font-semibold",
              unread ? "text-foreground" : "text-muted-foreground",
            )}
          >
            {mail.subject ?? "(no subject)"}
          </span>
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
        Floating controls: only ever offered on hover or keyboard focus, a
        momentary interaction, so they stay positioned over the row rather
        than reserved in its flex layout. Star and read/unread are not in
        this group: they live in the header line above instead, in a slot
        that's never covering text -- which is why this group is anchored
        from the top (`top-9`) rather than from the bottom: the row's
        shortest shape has no room to spare below the header line, and a
        bottom anchor's own position shifts with row height, so it can end
        up right against that line rather than clear of it. All of them sit
        in one row rather than Delete stacked in a row of its own above
        this one, for the same reason: no clearance left over for a second
        band. Delete keeps a wider gap from its neighbours instead, and its
        own hover colour, so a reach for Archive or Junk doesn't land on it
        by mistake. Every button here stays in the DOM at all times -- only
        opacity/pointer-events toggle on hover or focus -- so nothing shifts
        under the pointer as the row reveals itself.
      */}
      <div className="pointer-events-none absolute top-9 right-2 flex items-center gap-0.5">
        <button
          className={cn(
            "pointer-events-auto rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
            revealOnHoverClass,
          )}
          onClick={(e) => {
            e.stopPropagation();
            act("archive");
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
              act("not_spam");
            }}
            title={`Remove from Junk${threadSuffix}`}
            aria-label={`Remove from Junk${threadSuffix}`}
          >
            <Undo2 className="h-4 w-4 text-muted-foreground" />
          </button>
        ) : (
          <button
            className={cn(
              "pointer-events-auto rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors",
              revealOnHoverClass,
            )}
            onClick={(e) => {
              e.stopPropagation();
              act("spam");
            }}
            title={`Move to Junk${threadSuffix}`}
            aria-label={`Move to Junk${threadSuffix}`}
          >
            <Ban className="h-4 w-4 text-muted-foreground" />
          </button>
        )}
        <button
          className={cn(
            "pointer-events-auto ml-2 rounded-md bg-background/95 p-1.5 text-muted-foreground hover:bg-muted hover:text-destructive transition-colors",
            revealOnHoverClass,
          )}
          onClick={(e) => {
            e.stopPropagation();
            act("trash");
          }}
          title={`Move to trash${threadSuffix}`}
          aria-label={`Move to trash${threadSuffix}`}
        >
          <Trash2 className="h-4 w-4 text-muted-foreground" />
        </button>
      </div>
    </div>
  );
}
