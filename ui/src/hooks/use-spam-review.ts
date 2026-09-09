/** TanStack Query hooks for the spam review screen -- every message whose
 * latest verdict calls it spam with no user ruling since, across every
 * account and folder. See use-verdicts.ts: the same ruling endpoint every
 * other surface calls, which records the correction and moves the message
 * to match in one call. */

import { useCallback } from "react";
import { type InfiniteData, useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
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
 * Record a decision on one review item and drop it from the list. Thumb
 * up (`agree: true`) confirms the spam verdict, moving the message to
 * Junk; thumb down (`agree: false`) corrects it, moving the message back
 * to the inbox. One call does both the record and the move regardless of
 * where the message currently sits -- a message the pipeline never moved
 * (auto-move-to-junk is off) is already in the inbox, so a reject there
 * is a no-op move, not a special case to route around.
 */
export function useSpamReviewDecision() {
  const qc = useQueryClient();
  const verdictFeedback = useVerdictFeedback();

  const decide = useCallback(
    async (item: SpamReviewItem, agree: boolean) => {
      await verdictFeedback.mutateAsync({
        mailId: item.message_id,
        accountId: item.account_id,
        isSpam: agree,
      });
      removeFromReviewCache(qc, item.message_id);
    },
    [qc, verdictFeedback],
  );

  return { decide, isPending: verdictFeedback.isPending };
}
