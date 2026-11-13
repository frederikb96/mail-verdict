"use client";

import { useMemo, useState } from "react";
import { Loader2 } from "lucide-react";

import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { ExampleMailPicker } from "@/components/rules/example-mail-picker";
import { TraceStage } from "@/components/pipeline/run-detail-sheet";
import { OptionSelect } from "@/components/settings/setting-fields";
import { usePipeline, useTestPipeline, useTestStage } from "@/hooks/use-pipeline";
import type { PipelineTestOrigin, PipelineTraceEntry } from "@/types/api";

const ORIGIN_OPTIONS = [
  { value: "live", label: "Newly arrived" },
  { value: "historical", label: "Already in the mailbox" },
];

/**
 * Dry-run tester: nothing applied or persisted, for all of your rules or
 * one. Pick one of your recent mails and see which rules would act on it
 * and what each would do.
 */
export function PipelineTestDialog({
  open,
  onOpenChange,
  stageId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Undefined tests every rule; set tests just that one. */
  stageId?: string;
}) {
  const [messageId, setMessageId] = useState<string | null>(null);
  const [origin, setOrigin] = useState<PipelineTestOrigin>("live");
  const testPipeline = useTestPipeline();
  const testStage = useTestStage();
  const { data: pipeline } = usePipeline();
  const names = useMemo(
    () => new Map((pipeline?.stages ?? []).map((s) => [s.stage_id, s.name])),
    [pipeline],
  );

  const pending = testPipeline.isPending || testStage.isPending;
  const whole = testPipeline.data;
  const entries: PipelineTraceEntry[] | null = stageId
    ? testStage.data
      ? [testStage.data as unknown as PipelineTraceEntry]
      : null
    : (whole?.trace ?? null);
  const error = testPipeline.error ?? testStage.error;
  const title = stageId ? `Test “${names.get(stageId) ?? stageId}”` : "Test your rules";

  const handleRun = () => {
    if (!messageId) return;
    if (stageId) {
      testStage.mutate({ stageId, data: { message_id: messageId, origin } });
    } else {
      testPipeline.mutate({ message_id: messageId, origin });
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) {
          testPipeline.reset();
          testStage.reset();
          setMessageId(null);
        }
        onOpenChange(o);
      }}
    >
      <DialogContent size="xl" className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          Choose a mail to try {stageId ? "this rule" : "your rules"} on. Nothing is changed and
          nothing is saved.
        </p>
        <ExampleMailPicker selectedId={messageId} onPick={(m) => setMessageId(m.id)} />
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">Options</summary>
          <div className="mt-2 flex items-center gap-2">
            <Label className="text-sm">Treat the mail as</Label>
            <OptionSelect
              aria-label="Treat the mail as"
              value={origin}
              options={ORIGIN_OPTIONS}
              className="w-56"
              onChange={(v) => setOrigin(v as PipelineTestOrigin)}
            />
          </div>
        </details>
        <div className="flex justify-end">
          <Button onClick={handleRun} disabled={pending || !messageId}>
            {pending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
            Run test
          </Button>
        </div>

        {error && (
          <p className="text-sm text-destructive">
            {error instanceof ApiError ? error.message : String(error)}
          </p>
        )}

        {entries && (
          <div className="flex flex-col gap-2">
            {!stageId && whole && (
              <p className="text-sm">
                Result: <span className="font-medium">{whole.status}</span>
                {whole.skip_reason ? ` — ${whole.skip_reason}` : ""}
              </p>
            )}
            {entries.map((entry, i) => (
              <TraceStage key={i} entry={entry} names={names} />
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
