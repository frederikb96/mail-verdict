"use client";

import { useState } from "react";
import { Loader2 } from "lucide-react";

import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import { useTestPipeline, useTestStage } from "@/hooks/use-pipeline";
import type { PipelineTestOrigin } from "@/types/api";

/**
 * Dry-run tester: nothing applied or persisted, either for the whole
 * pipeline or one stage. Needs an existing message's id -- there is no
 * message picker here, only a paste field, since finding one is a quick
 * copy from the mail list's URL or the runs tail.
 */
export function PipelineTestDialog({
  open,
  onOpenChange,
  stageId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Undefined tests the whole pipeline; set tests just that one stage. */
  stageId?: string;
}) {
  const [messageId, setMessageId] = useState("");
  const [origin, setOrigin] = useState<PipelineTestOrigin>("live");
  const testPipeline = useTestPipeline();
  const testStage = useTestStage();

  const pending = testPipeline.isPending || testStage.isPending;
  const result = stageId ? testStage.data : testPipeline.data;
  const error = testPipeline.error ?? testStage.error;

  const handleRun = () => {
    if (!messageId.trim()) return;
    if (stageId) {
      testStage.mutate({ stageId, data: { message_id: messageId.trim(), origin } });
    } else {
      testPipeline.mutate({ message_id: messageId.trim(), origin });
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) {
          testPipeline.reset();
          testStage.reset();
          setMessageId("");
        }
        onOpenChange(o);
      }}
    >
      <DialogContent className="max-h-[85vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {stageId ? `Test stage “${stageId}”` : "Test the whole pipeline"}
          </DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          Dry-runs against an existing message. Nothing is applied or persisted.
        </p>
        <div className="flex gap-2">
          <div className="flex-1 grid gap-1.5">
            <Label className="text-sm">Message id</Label>
            <Input
              value={messageId}
              onChange={(e) => setMessageId(e.target.value)}
              placeholder="paste a message UUID"
            />
          </div>
          <div className="grid gap-1.5">
            <Label className="text-sm">Origin</Label>
            <Select value={origin} onValueChange={(v) => setOrigin(v as PipelineTestOrigin)}>
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="live">live</SelectItem>
                <SelectItem value="historical">historical</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="flex justify-end">
          <Button onClick={handleRun} disabled={pending || !messageId.trim()}>
            {pending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
            Run
          </Button>
        </div>

        {error && (
          <p className="text-sm text-destructive">
            {error instanceof ApiError ? error.message : String(error)}
          </p>
        )}

        {result && (
          <div className="rounded-md border bg-muted/30 p-2">
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap text-xs">
              {JSON.stringify(result, null, 2)}
            </pre>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
