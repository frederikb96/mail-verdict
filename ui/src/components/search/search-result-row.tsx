"use client";

/**
 * A lighter row than the mail list's own MailListItem: a search result
 * carries no seen/flagged/account/folder-derived actions (star, archive,
 * spam, trash) the way a mail list row does, only enough to show read and
 * flagged state and open the message in context. Opening a hit resolves
 * the rest (account, folder, thread) from the message itself -- see
 * search-page.tsx's openResult mutation.
 */

import { Star } from "lucide-react";
import { cn } from "@/lib/utils";
import { extractEmail, formatRelativeDate, extractSenderName } from "@/lib/format";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { useContactPhotoIndex } from "@/hooks/use-contacts";
import type { SearchResultItem } from "@/hooks/use-search";

/**
 * The search API highlights matches with `**...**`; body/subject text
 * isn't guaranteed free of stray markup, so tags are stripped here too
 * before splitting on the markers.
 */
function renderSnippet(snippet: string) {
  const plain = snippet.replace(/<\/?[^>]+>/g, "");
  const parts = plain.split(/\*\*(.+?)\*\*/g);
  return parts.map((part, i) =>
    i % 2 === 1 ? <strong key={i}>{part}</strong> : part,
  );
}

interface SearchResultRowProps {
  result: SearchResultItem;
  onOpen: (messageId: string) => void;
}

export function SearchResultRow({ result, onOpen }: SearchResultRowProps) {
  const senderName = extractSenderName(result.from_addr);

  // One request per account rendered (deduped/cached by TanStack Query
  // across every row sharing it, never one per row), the same lookup
  // mail-list-item.tsx reads its own avatar photo from.
  const { data: photoIndex } = useContactPhotoIndex(result.account_id);
  const senderEmail = extractEmail(result.from_addr).toLowerCase();
  const photoUrl = photoIndex?.by_email[senderEmail]?.photo_url ?? null;

  return (
    <button
      type="button"
      data-testid="search-result-row"
      data-message-id={result.id}
      onClick={() => onOpen(result.id)}
      className="flex w-full items-start gap-3 border-b px-4 py-3 text-left hover:bg-accent/50"
    >
      <InitialsAvatar name={senderName} photoUrl={photoUrl} className="mt-0.5" />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center gap-2">
          {!result.is_seen && <div className="h-2 w-2 shrink-0 rounded-full bg-blue-500" />}
          <span
            className={cn(
              "truncate text-sm text-foreground",
              !result.is_seen ? "font-semibold" : "font-medium",
            )}
          >
            {senderName}
          </span>
          {result.is_flagged && (
            <Star className="h-3 w-3 shrink-0 fill-amber-400 text-amber-400" />
          )}
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">
            {formatRelativeDate(result.received_at)}
          </span>
        </div>
        <span
          className={cn(
            "truncate text-sm",
            !result.is_seen ? "font-semibold text-foreground" : "text-foreground/90",
          )}
        >
          {result.subject || "(no subject)"}
        </span>
        {result.snippet && (
          <span className="truncate text-xs text-muted-foreground">
            {renderSnippet(result.snippet)}
          </span>
        )}
      </div>
    </button>
  );
}
