"use client";

import { useState } from "react";
import { History } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";

import { FailuresTable } from "@/components/pipeline/failures-table";
import { QueueCard } from "@/components/pipeline/queue-card";
import { RevisionsDialog } from "@/components/pipeline/revisions-dialog";
import { RunsTail } from "@/components/pipeline/runs-tail";
import { StageList } from "@/components/pipeline/stage-list";

import { usePipeline, useSetPipelineEnabled, useStageTypes } from "@/hooks/use-pipeline";
import { useQueues } from "@/hooks/use-queues";

export function PipelinePage() {
  const { data: pipeline, isLoading: pipelineLoading } = usePipeline();
  const { data: stageTypes } = useStageTypes();
  const { data: queues, isLoading: queuesLoading } = useQueues();
  const setEnabled = useSetPipelineEnabled();
  const [revisionsOpen, setRevisionsOpen] = useState(false);

  return (
    <div className="flex flex-col gap-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Pipeline</h1>
          <p className="text-sm text-muted-foreground">
            Every message is embedded, then run through this stage list. Spam
            classification is a stage; a rule is a <span className="font-mono">match</span> stage.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {pipeline && (
            <>
              <span className="text-xs text-muted-foreground">rev {pipeline.revision}</span>
              <Button variant="outline" size="sm" onClick={() => setRevisionsOpen(true)}>
                <History className="mr-1 h-3.5 w-3.5" />
                History
              </Button>
              <div className="flex items-center gap-2">
                <span className="text-sm">Enabled</span>
                <Switch
                  checked={pipeline.enabled}
                  onCheckedChange={(checked: boolean) =>
                    setEnabled.mutate({ enabled: checked, baseRevision: pipeline.revision })
                  }
                />
              </div>
            </>
          )}
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {queuesLoading &&
          [0, 1].map((i) => <Skeleton key={i} className="h-48 w-full" />)}
        {queues?.map((queue) => (
          <QueueCard key={queue.name} queue={queue} />
        ))}
      </div>

      {pipelineLoading && <Skeleton className="h-64 w-full" />}
      {pipeline && stageTypes && (
        <StageList pipeline={pipeline} stageTypes={stageTypes} />
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <FailuresTable />
        <RunsTail />
      </div>

      {pipeline && (
        <RevisionsDialog
          open={revisionsOpen}
          onOpenChange={setRevisionsOpen}
          currentRevision={pipeline.revision}
        />
      )}
    </div>
  );
}
