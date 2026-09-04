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
import { formatRelativeDate, extractSenderName } from "@/lib/format";
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

  return (
    <button
      type="button"
      data-testid="search-result-row"
      data-message-id={result.message_id}
      onClick={() => onOpen(result.message_id)}
      className="flex w-full flex-col gap-1 border-b px-4 py-3 text-left hover:bg-accent/50"
    >
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
    </button>
  );
}
