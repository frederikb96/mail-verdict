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
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-3">
        <CardTitle className="text-base">Failures</CardTitle>
        {runs && runs.length > 0 && (
          <Button
            variant="outline"
            size="sm"
            disabled={retryingAll}
            onClick={handleRetryAll}
          >
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
                className="flex cursor-pointer items-center gap-3 px-4 py-2 text-sm hover:bg-muted/50"
                onClick={() => setOpenRunId(run.id)}
              >
                <div className="min-w-0 flex-1 truncate">
                  <RunSubject messageId={run.message_id} msgKey={run.msg_key} />
                </div>
                <span className="w-32 shrink-0 truncate font-mono text-xs text-muted-foreground">
                  {run.failed_stage ?? "—"}
                </span>
                <span className="w-64 shrink-0 truncate text-xs text-destructive">
                  {run.last_error ?? "—"}
                </span>
                <span className="w-14 shrink-0 text-right text-xs text-muted-foreground">
                  {formatRelativeDate(run.finished_at ?? run.created_at)}
                </span>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  disabled={retryRun.isPending}
                  onClick={(e) => {
                    e.stopPropagation();
                    retryRun.mutate(run.id);
                  }}
                  title="Retry this run"
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
