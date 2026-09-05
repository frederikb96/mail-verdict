"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { VList, type VListHandle } from "virtua";
import { Ban, CheckCheck, Loader2, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { SpamReviewRow } from "@/components/mail/spam-review-row";
import { useSpamReviewDecision, useSpamReviewList } from "@/hooks/use-spam-review";
import { useToast } from "@/hooks/use-toast";
import type { SpamReviewItem } from "@/types/api";

/** How near the bottom, in px, triggers the next page -- same threshold
 * the mail list and search page use for their own VList. */
const LOAD_MORE_MARGIN = 200;

type BulkKind = "accept" | "reject";

/**
 * Everything the classifier currently calls spam with no ruling on it yet,
 * across every account and folder including Junk -- a view over verdicts,
 * not a folder. Nothing here moves a message except a rejected verdict on
 * one already sitting in Junk (see use-spam-review.ts).
 */
export function SpamReviewPage() {
  const { data, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage } = useSpamReviewList();
  const { decide, isPending } = useSpamReviewDecision();
  const { push: pushToast } = useToast();
  const vlistRef = useRef<VListHandle>(null);
  const [pendingBulk, setPendingBulk] = useState<BulkKind | null>(null);
  const [bulkRunning, setBulkRunning] = useState(false);

  const items = useMemo(() => data?.pages.flatMap((p) => p.items) ?? [], [data]);

  const handleScroll = useCallback(
    (offset: number) => {
      if (!vlistRef.current) return;
      const { scrollSize, viewportSize } = vlistRef.current;
      if (
        scrollSize - offset - viewportSize < LOAD_MORE_MARGIN &&
        hasNextPage &&
        !isFetchingNextPage
      ) {
        fetchNextPage();
      }
    },
    [hasNextPage, isFetchingNextPage, fetchNextPage],
  );

  // "Everything currently listed" -- the loaded batch, not the whole
  // matching set, the same scope a folder-wide predicate explicitly is
  // not needed for here: verdicts are corrected one at a time server-side
  // and there is nothing like a bulk-action scope for this query.
  const runBulk = useCallback(
    async (kind: BulkKind) => {
      const batch: SpamReviewItem[] = items;
      setBulkRunning(true);
      try {
        const results = await Promise.allSettled(
          batch.map((item) => decide(item, kind === "accept")),
        );
        const failed = results.filter((r) => r.status === "rejected").length;
        if (failed > 0) {
          pushToast(
            `${failed} of ${batch.length} could not be ${kind === "accept" ? "confirmed" : "corrected"}`,
            "error",
            0,
          );
        } else {
          pushToast(
            `${batch.length} message${batch.length === 1 ? "" : "s"} ${kind === "accept" ? "confirmed as spam" : "corrected"}`,
            "success",
          );
        }
      } finally {
        setBulkRunning(false);
      }
    },
    [items, decide, pushToast],
  );

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Spam review</h1>
        {!isLoading && <Badge variant="secondary">{items.length} to review</Badge>}
        <div className="ml-auto flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={items.length === 0 || bulkRunning || isPending}
            onClick={() => setPendingBulk("accept")}
          >
            <CheckCheck className="h-4 w-4" />
            Accept all
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={items.length === 0 || bulkRunning || isPending}
            onClick={() => setPendingBulk("reject")}
          >
            <Ban className="h-4 w-4" />
            Reject all
          </Button>
        </div>
      </div>

      <div className="flex-1 overflow-hidden rounded-lg border">
        {isLoading && (
          <div className="flex flex-col gap-3 p-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
        )}

        {!isLoading && items.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
            <ShieldCheck className="h-12 w-12 opacity-50" />
            <p>Nothing to review</p>
          </div>
        )}

        {!isLoading && items.length > 0 && (
          <VList ref={vlistRef} className="h-full" itemSize={96} onScroll={handleScroll}>
            {items.map((item) => (
              <SpamReviewRow
                key={item.message_id}
                item={item}
                onDecide={decide}
                disabled={isPending || bulkRunning}
              />
            ))}
          </VList>
        )}

        {isFetchingNextPage && (
          <div className="flex items-center justify-center py-3">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          </div>
        )}
      </div>

      <ConfirmDialog
        open={pendingBulk !== null}
        onOpenChange={(open) => {
          if (!open) setPendingBulk(null);
        }}
        title={
          pendingBulk === "accept"
            ? `Confirm ${items.length} message${items.length === 1 ? "" : "s"} as spam?`
            : `Correct ${items.length} message${items.length === 1 ? "" : "s"}?`
        }
        description={
          pendingBulk === "accept"
            ? "Records agreement with the classifier for every message currently listed. Nothing moves."
            : "Records a correction for every message currently listed, and moves any of them still sitting in Junk back to the inbox."
        }
        confirmLabel={pendingBulk === "accept" ? "Accept all" : "Reject all"}
        confirmVariant="default"
        isConfirming={bulkRunning}
        onConfirm={() => {
          const kind = pendingBulk;
          setPendingBulk(null);
          if (kind) void runBulk(kind);
        }}
      />
    </div>
  );
}
