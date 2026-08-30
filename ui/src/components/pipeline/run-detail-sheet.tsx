"use client";

import { CheckCircle2, Loader2, RotateCw, XCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

import { RunStatusBadge } from "@/components/pipeline/run-status-badge";
import { RunSubject } from "@/components/pipeline/run-subject";
import { formatFullDate } from "@/lib/format";
import { useRetryRun, useRun } from "@/hooks/use-runs";
import type { PipelineTraceEntry } from "@/types/api";

function TraceStage({ entry }: { entry: PipelineTraceEntry }) {
  return (
    <div className="rounded-md border p-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm font-medium">{entry.stage_id}</span>
        <span className="text-xs text-muted-foreground">{entry.type}</span>
        {entry.matched === false ? (
          <XCircle className="h-3.5 w-3.5 text-muted-foreground" />
        ) : (
          <CheckCircle2 className="h-3.5 w-3.5 text-green-600" />
        )}
        {entry.halt && <span className="text-xs text-muted-foreground">halted</span>}
        {entry.usage?.latency_ms != null && (
          <span className="ml-auto text-xs text-muted-foreground">
            {entry.usage.model} &bull; {Math.round(entry.usage.latency_ms)}ms
          </span>
        )}
      </div>
      {entry.detail && <p className="mt-1 text-sm">{entry.detail}</p>}
      {entry.applied && entry.applied.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1">
          {entry.applied.map((a, i) => (
            <li key={i} className="flex min-w-0 items-start gap-1.5 text-xs">
              {a.applied ? (
                <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-green-600" />
              ) : (
                <XCircle className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
              )}
              <span className="min-w-0 break-all font-mono">
                {JSON.stringify(a.effect)}
                {a.detail && (
                  <span className="text-muted-foreground"> — {a.detail}</span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function RunDetailSheet({
  runId,
  open,
  onOpenChange,
}: {
  runId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { data: run, isLoading } = useRun(open ? runId : null);
  const retryRun = useRetryRun();

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-[32rem] sm:max-w-[32rem]">
        <SheetHeader>
          <SheetTitle>Run trace</SheetTitle>
        </SheetHeader>
        <div className="flex flex-col gap-4 overflow-y-auto px-4 pb-4">
          {isLoading && (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-6 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          )}
          {run && (
            <>
              <div className="flex items-center gap-2">
                <RunStatusBadge status={run.status} />
                {run.status === "failed" && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={retryRun.isPending}
                    onClick={() => retryRun.mutate(run.id)}
                  >
                    {retryRun.isPending ? (
                      <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                    ) : (
                      <RotateCw className="mr-1 h-3 w-3" />
                    )}
                    Retry
                  </Button>
                )}
              </div>

              <div className="flex flex-col gap-1 text-sm">
                <RunSubject messageId={run.message_id} msgKey={run.msg_key} />
                <span className="text-xs text-muted-foreground">
                  {run.origin} &bull; pipeline rev {run.pipeline_rev ?? "?"} &bull;{" "}
                  {run.attempts} attempt{run.attempts === 1 ? "" : "s"}
                  {run.finished_at ? ` • ${formatFullDate(run.finished_at)}` : ""}
                </span>
              </div>

              {run.skip_reason && (
                <p className="text-sm text-muted-foreground">Skipped: {run.skip_reason}</p>
              )}
              {run.last_error && (
                <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-sm text-destructive">
                  {run.failed_stage ? `[${run.failed_stage}] ` : ""}
                  {run.last_error}
                </p>
              )}
              {run.halted_at_stage && (
                <p className="text-xs text-muted-foreground">
                  Halted at <span className="font-mono">{run.halted_at_stage}</span>
                </p>
              )}

              <div className="flex flex-col gap-2">
                {run.trace.length === 0 && (
                  <p className="text-sm text-muted-foreground">No stages ran.</p>
                )}
                {run.trace.map((entry, i) => (
                  <TraceStage key={i} entry={entry} />
                ))}
              </div>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
