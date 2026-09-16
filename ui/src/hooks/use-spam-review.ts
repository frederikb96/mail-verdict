/** TanStack Query hooks for the spam review screen -- every message whose
 * latest verdict calls it spam with no user ruling since, across every
 * account and folder. A decision is the same spam / not-spam mail action
 * every other surface takes (use-mail-intents.ts), which records the ruling
 * and moves the message to match in one call. */

import { useCallback, useMemo } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useProjectionIntents } from "@/hooks/use-intent-ledger";
import { useMailAction } from "@/hooks/use-mail-intents";
import { appliesTo } from "@/lib/mail-intents";
import { readTimeOf, timedRead } from "@/lib/read-clock";
import type { SpamReviewItem } from "@/types/api";

export const spamReviewKeys = {
  list: ["spam-review"] as const,
};

/** Newest-verdict-first, paginated the same shape the mail list uses --
 * less every message a ruling is already on its way for. */
export function useSpamReviewList() {
  const query = useInfiniteQuery({
    queryKey: spamReviewKeys.list,
    queryFn: ({ pageParam, queryKey, signal }: {
      pageParam: string | null; queryKey: readonly unknown[]; signal: AbortSignal;
    }) =>
      timedRead(queryKey, signal, pageParam !== null, () =>
        api.verdicts.spamReview({ before: pageParam ?? undefined, limit: 50 })),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : undefined),
  });
  const intents = useProjectionIntents();
  const readAt = readTimeOf(spamReviewKeys.list, query.dataUpdatedAt);
  const items = useMemo(() => {
    const all = query.data?.pages.flatMap((p) => p.items) ?? [];
    const ruled = new Set(
      intents
        .filter((i) => (i.action === "spam" || i.action === "not_spam") && appliesTo(i, readAt))
        .flatMap((i) => i.messages.map((m) => m.id)),
    );
    return ruled.size === 0 ? all : all.filter((item) => !ruled.has(item.message_id));
  }, [query.data, intents, readAt]);
  return { ...query, items };
}

/**
 * Decide on review items: agreeing confirms the spam verdict and files the
 * message in Junk, disagreeing corrects it and rescues the message from
 * Junk when it is there. One mail action per account, however many items.
 */
export function useSpamReviewDecision() {
  const { performAll } = useMailAction();
  const decide = useCallback(
    (items: SpamReviewItem[], agree: boolean) => {
      const byAccount = new Map<string, SpamReviewItem[]>();
      for (const item of items) {
        byAccount.set(item.account_id, [...(byAccount.get(item.account_id) ?? []), item]);
      }
      performAll(
        [...byAccount.entries()].map(([accountId, group]) => ({
          accountId,
          mailIds: group.map((item) => item.message_id),
          action: agree ? "spam" : "not_spam",
          bulk: true,
          seenFolderIds: Object.fromEntries(group.map((item) => [item.message_id, item.folder_id])),
        })),
      );
    },
    [performAll],
  );
  return { decide };
}
