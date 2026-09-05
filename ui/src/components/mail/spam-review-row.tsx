"use client";

import { ThumbsDown, ThumbsUp } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { extractSenderName, formatRelativeDate } from "@/lib/format";
import type { SpamReviewItem } from "@/types/api";

interface SpamReviewRowProps {
  item: SpamReviewItem;
  onDecide: (item: SpamReviewItem, agree: boolean) => void;
  disabled: boolean;
}

/** One undecided spam verdict: the message, the reasoning that flagged
 * it, and the accept/reject controls -- accept records agreement with no
 * move, reject records the correction and (see use-spam-review.ts) moves
 * the message back to the inbox when the pipeline had already moved it
 * to Junk. */
export function SpamReviewRow({ item, onDecide, disabled }: SpamReviewRowProps) {
  const senderName = extractSenderName(item.from_addr);

  return (
    <div className="flex items-start gap-3 border-b px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium text-foreground">{senderName}</span>
          {item.is_junk && (
            <Badge variant="secondary" className="h-4 shrink-0 px-1 text-[10px]">
              Junk
            </Badge>
          )}
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">
            {formatRelativeDate(item.received_at)}
          </span>
        </div>
        <div className="truncate text-sm text-muted-foreground">
          {item.subject ?? "(no subject)"}
        </div>
        {item.reasoning && (
          <p className="mt-1 line-clamp-2 text-xs text-muted-foreground italic">
            {item.reasoning}
          </p>
        )}
        {item.model_used && (
          <span className="text-[10px] text-muted-foreground">{item.model_used}</span>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() => onDecide(item, true)}
          title="Yes, this is spam"
          aria-label="Yes, this is spam"
        >
          <ThumbsUp className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() => onDecide(item, false)}
          title="Not spam"
          aria-label="Not spam"
        >
          <ThumbsDown className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
