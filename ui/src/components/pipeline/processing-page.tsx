"use client";

/**
 * Admin > Processing: what an operator checks first. Queues, then the failed
 * runs of the admin's own accounts, then a count-only summary of every
 * person's rule set (never its content, failures counted), then the live tail
 * of the admin's own recent runs. Another person's runs carry their mail, so
 * they appear here only as counts.
 */

import { Users } from "lucide-react";

import { CategoryHeader, SectionCard } from "@/components/settings/blocks";
import { FailuresTable } from "@/components/pipeline/failures-table";
import { QueueCard } from "@/components/pipeline/queue-card";
import { RunsTail } from "@/components/pipeline/runs-tail";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useAdminRuleSets } from "@/hooks/use-admin";
import { useQueues } from "@/hooks/use-queues";
import { formatRelativeAgo } from "@/lib/format";
import type { AdminRuleSet } from "@/types/api";

function RuleSetRow({ set }: { set: AdminRuleSet }) {
  const paused = set.enabled === false;
  return (
    <div
      data-testid="rule-set-row"
      className="flex flex-col gap-0.5 border-b py-2.5 first:pt-0 last:border-b-0 last:pb-0 sm:flex-row sm:items-center sm:gap-4"
    >
      <div className="min-w-0 break-words text-sm font-medium sm:w-48 sm:shrink-0">
        {set.display_name}
      </div>
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {set.revision === null ? (
          <span>No rules opened yet</span>
        ) : (
          <>
            <span>
              {set.stages} rule{set.stages === 1 ? "" : "s"}, {set.enabled_stages} on
            </span>
            <span>revision {set.revision}</span>
            {set.changed_at && <span>changed {formatRelativeAgo(set.changed_at)}</span>}
          </>
        )}
        {paused && <Badge variant="outline">paused</Badge>}
        {set.failed_runs > 0 && <Badge variant="destructive">{set.failed_runs} failed</Badge>}
      </div>
    </div>
  );
}

export function ProcessingPage() {
  const { data: queues, isLoading: queuesLoading } = useQueues();
  const { data: ruleSets, isLoading: ruleSetsLoading } = useAdminRuleSets();

  return (
    <div className="flex flex-col gap-4">
      <CategoryHeader
        title="Processing"
        description="Every message is embedded, then run through its owner's rules. The queues cover the whole instance; failures and recent runs are your own accounts', and everyone else's failures are counted under Rule sets."
      />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {queuesLoading && [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-28 w-full" />)}
        {queues?.map((queue) => (
          <QueueCard key={queue.name} queue={queue} />
        ))}
      </div>

      <FailuresTable />

      <SectionCard
        icon={Users}
        title="Rule sets"
        description="One per person: how many rules, whether they are paused, and when they last changed. Counts only; what the rules say stays with their owner."
      >
        {ruleSetsLoading && <Skeleton className="h-16 w-full" />}
        {ruleSets?.length === 0 && <p className="text-sm text-muted-foreground">No users yet.</p>}
        <div>
          {ruleSets?.map((set) => <RuleSetRow key={set.user_id} set={set} />)}
        </div>
      </SectionCard>

      <RunsTail />
    </div>
  );
}
