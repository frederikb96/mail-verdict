/** TanStack Query hooks for the spam review screen -- every message whose
 * latest verdict calls it spam with no user ruling since, across every
 * account and folder. See use-verdicts.ts (the correction loop this
 * reuses) and use-mails.ts's useMailAction (the move-back-to-inbox this
 * reuses for a rejected verdict on a message already in Junk). */

import { useCallback } from "react";
import { type InfiniteData, useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useMailAction } from "@/hooks/use-mails";
import { useVerdictFeedback } from "@/hooks/use-verdicts";
import type { SpamReviewItem, SpamReviewListResponse } from "@/types/api";

export const spamReviewKeys = {
  list: ["spam-review"] as const,
};

/** Newest-verdict-first, paginated the same shape the mail list uses. */
export function useSpamReviewList() {
  return useInfiniteQuery({
    queryKey: spamReviewKeys.list,
    queryFn: ({ pageParam }: { pageParam: string | null }) =>
      api.verdicts.spamReview({ before: pageParam ?? undefined, limit: 50 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : undefined),
  });
}

function removeFromReviewCache(
  qc: ReturnType<typeof useQueryClient>,
  messageId: string,
) {
  qc.setQueriesData<InfiniteData<SpamReviewListResponse>>(
    { queryKey: spamReviewKeys.list },
    (old) => {
      if (!old) return old;
      return {
        ...old,
        pages: old.pages.map((page) => ({
          ...page,
          items: page.items.filter((item) => item.message_id !== messageId),
        })),
      };
    },
  );
}

/**
 * Record a decision on one review item and drop it from the list. Thumb up
 * (`agree: true`) always just records agreement -- nothing moves. Thumb
 * down (`agree: false`) records the correction too, and additionally moves
 * the message back to the inbox when it was sitting in Junk (`is_junk`) --
 * reusing the existing "not_spam" action wholesale rather than duplicating
 * its folder-count and cache bookkeeping, since that action already does
 * both the move and the feedback write in one call. A message the pipeline
 * never moved (still in whatever folder it arrived in) has nothing to move
 * back from, so a thumb-down there is feedback only, exactly like thumb up.
 */
export function useSpamReviewDecision() {
  const qc = useQueryClient();
  const mailAction = useMailAction();
  const verdictFeedback = useVerdictFeedback();

  const decide = useCallback(
    async (item: SpamReviewItem, agree: boolean) => {
      if (!agree && item.is_junk) {
        await mailAction.mutateAsync({
          mailId: item.message_id,
          accountId: item.account_id,
          action: { action: "not_spam" },
        });
      } else {
        await verdictFeedback.mutateAsync({
          mailId: item.message_id,
          accountId: item.account_id,
          isSpam: agree,
        });
      }
      removeFromReviewCache(qc, item.message_id);
    },
    [qc, mailAction, verdictFeedback],
  );

  return { decide, isPending: mailAction.isPending || verdictFeedback.isPending };
}
