"use client";

import { Star, Archive, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  extractSenderName,
  formatRelativeDate,
  getInitials,
} from "@/lib/format";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import type { MailSummary } from "@/types/api";

interface MailListItemProps {
  mail: MailSummary;
  isSelected: boolean;
  onSelect: (mailId: string) => void;
  onAction?: (
    mailId: string,
    action: "flag" | "unflag" | "delete",
  ) => void;
}

export function MailListItem({
  mail,
  isSelected,
  onSelect,
  onAction,
}: MailListItemProps) {
  const senderName = extractSenderName(mail.from_addr);
  const initials = getInitials(senderName);

  return (
    <div
      className={cn(
        "group flex h-16 cursor-pointer items-center gap-3 border-b px-3 transition-colors",
        isSelected
          ? "bg-accent"
          : "hover:bg-accent/50",
        !mail.is_read && "bg-accent/20",
      )}
      onClick={() => onSelect(mail.id)}
    >
      {/* Avatar */}
      <Avatar className="h-8 w-8 shrink-0">
        <AvatarFallback className="text-xs">{initials}</AvatarFallback>
      </Avatar>

      {/* Content */}
      <div className="flex min-w-0 flex-1 flex-col justify-center">
        <div className="flex items-center gap-2">
          {/* Unread dot */}
          {!mail.is_read && (
            <div className="h-2 w-2 shrink-0 rounded-full bg-blue-500" />
          )}
          <span
            className={cn(
              "truncate text-sm",
              !mail.is_read && "font-semibold",
            )}
          >
            {senderName}
          </span>
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">
            {formatRelativeDate(mail.received_at)}
          </span>
        </div>
        <div className="truncate text-sm text-foreground">
          {mail.subject ?? "(no subject)"}
        </div>
      </div>

      {/* Hover actions */}
      <div className="hidden shrink-0 items-center gap-1 group-hover:flex">
        <button
          className="rounded p-1 hover:bg-accent"
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(
              mail.id,
              mail.is_flagged ? "unflag" : "flag",
            );
          }}
          title={mail.is_flagged ? "Unflag" : "Flag"}
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
          className="rounded p-1 hover:bg-accent"
          onClick={(e) => {
            e.stopPropagation();
            onAction?.(mail.id, "delete");
          }}
          title="Delete"
        >
          <Trash2 className="h-4 w-4 text-muted-foreground" />
        </button>
      </div>

      {/* Star indicator (visible when not hovering) */}
      {mail.is_flagged && (
        <div className="shrink-0 group-hover:hidden">
          <Star className="h-4 w-4 fill-yellow-400 text-yellow-400" />
        </div>
      )}
    </div>
  );
}
