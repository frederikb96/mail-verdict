"use client";

import { useState } from "react";
import { CheckCircle2, Loader2, RotateCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

import { RunDetailSheet } from "@/components/pipeline/run-detail-sheet";
import { RunSubject } from "@/components/pipeline/run-subject";
import { formatRelativeDate } from "@/lib/format";
import { useRetryRun, useRuns } from "@/hooks/use-runs";

/** Failed runs, one row each: the message, where and why it failed, and a
 * retry. The row stacks on a narrow screen so no stage or error is cut off. */
export function FailuresTable() {
  const { data: runs, isLoading } = useRuns("failed");
  const retryRun = useRetryRun();
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [retryingAll, setRetryingAll] = useState(false);

  const handleRetryAll = async () => {
    if (!runs || runs.length === 0) return;
    setRetryingAll(true);
    for (const run of runs) {
      await retryRun.mutateAsync(run.id).catch(() => undefined);
    }
    setRetryingAll(false);
  };

  return (
    <Card id="failures">
      <CardHeader className="flex flex-row items-center justify-between pb-3">
        <CardTitle className="text-base">Failures</CardTitle>
        {runs && runs.length > 0 && (
          <Button variant="outline" size="sm" disabled={retryingAll} onClick={handleRetryAll}>
            {retryingAll ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : (
              <RotateCw className="mr-1 h-3 w-3" />
            )}
            Retry all ({runs.length})
          </Button>
        )}
      </CardHeader>
      <CardContent className="p-0">
        {isLoading && (
          <div className="flex flex-col gap-2 p-4">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        )}
        {!isLoading && (!runs || runs.length === 0) && (
          <div className="flex flex-col items-center gap-2 py-8 text-muted-foreground">
            <CheckCircle2 className="h-8 w-8 opacity-50" />
            <p className="text-sm">Nothing has failed</p>
          </div>
        )}
        {runs && runs.length > 0 && (
          <div className="divide-y">
            {runs.map((run) => (
              <div
                key={run.id}
                className="flex cursor-pointer items-start gap-3 px-4 py-2.5 text-sm hover:bg-muted/50"
                onClick={() => setOpenRunId(run.id)}
              >
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <div className="min-w-0 truncate">
                    <RunSubject messageId={run.message_id} msgKey={run.msg_key} />
                  </div>
                  <div className="min-w-0 break-words text-xs text-destructive">
                    {run.failed_stage && (
                      <span className="mr-1.5 font-mono text-muted-foreground">{run.failed_stage}</span>
                    )}
                    {run.last_error ?? "failed"}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {formatRelativeDate(run.finished_at ?? run.created_at)}
                  </span>
                </div>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  disabled={retryRun.isPending}
                  aria-label="Retry this run"
                  onClick={(e) => {
                    e.stopPropagation();
                    retryRun.mutate(run.id);
                  }}
                >
                  <RotateCw className="h-3.5 w-3.5" />
                </Button>
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
