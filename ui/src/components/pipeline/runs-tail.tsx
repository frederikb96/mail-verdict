"use client";

import { useState } from "react";
import { Radio } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

import { RunDetailSheet } from "@/components/pipeline/run-detail-sheet";
import { RunStatusBadge } from "@/components/pipeline/run-status-badge";
import { RunSubject } from "@/components/pipeline/run-subject";
import { formatRelativeDate } from "@/lib/format";
import { useRuns } from "@/hooks/use-runs";

/** The last fifty terminal runs, one line each -- the "why did this
 * message get that treatment" surface at a glance. */
export function RunsTail() {
  const { data: runs, isLoading } = useRuns(undefined, 50);
  const [openRunId, setOpenRunId] = useState<string | null>(null);

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Radio className="h-4 w-4" />
          Live tail
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading && (
          <div className="flex flex-col gap-2 p-4">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
          </div>
        )}
        {!isLoading && (!runs || runs.length === 0) && (
          <p className="p-4 text-sm text-muted-foreground">No runs yet.</p>
        )}
        {runs && runs.length > 0 && (
          <div className="max-h-[28rem] divide-y overflow-y-auto">
            {runs.map((run) => (
              <div
                key={run.id}
                className="flex cursor-pointer items-center gap-2 px-4 py-1.5 text-sm hover:bg-muted/50"
                onClick={() => setOpenRunId(run.id)}
              >
                <RunStatusBadge status={run.status} />
                <div className="min-w-0 flex-1 truncate">
                  <RunSubject messageId={run.message_id} msgKey={run.msg_key} />
                </div>
                {run.halted_at_stage && (
                  <span className="shrink-0 truncate font-mono text-xs text-muted-foreground">
                    halted @ {run.halted_at_stage}
                  </span>
                )}
                <span className="w-14 shrink-0 text-right text-xs text-muted-foreground">
                  {formatRelativeDate(run.finished_at ?? run.created_at)}
                </span>
              </div>
            ))}
          </div>
        )}
      </CardContent>

      <RunDetailSheet
        runId={openRunId}
        open={openRunId !== null}
        onOpenChange={(open) => !open && setOpenRunId(null)}
      />
    </Card>
  );
}
