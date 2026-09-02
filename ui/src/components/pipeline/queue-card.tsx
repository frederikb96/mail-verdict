"use client";

import { AlertTriangle, Minus, Pause, Play, Plus } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatFullDate } from "@/lib/format";
import { usePatchQueue } from "@/hooks/use-queues";
import type { QueueResponse } from "@/types/api";

const STATUS_ORDER = ["pending", "claimed", "failed", "done", "skipped", "cancelled"];
const STATUS_BADGE: Record<string, "outline" | "secondary" | "destructive"> = {
  pending: "outline",
  claimed: "secondary",
  failed: "destructive",
};

function orderedDepth(depth: Record<string, number>): [string, number][] {
  const known = STATUS_ORDER.filter((s) => s in depth).map(
    (s) => [s, depth[s]] as [string, number],
  );
  const rest = Object.entries(depth).filter(([k]) => !STATUS_ORDER.includes(k));
  return [...known, ...rest];
}

export function QueueCard({ queue }: { queue: QueueResponse }) {
  const patchQueue = usePatchQueue();
  const circuitOpen = queue.circuit.state !== "closed";
  const paused = queue.state === "paused";

  const setConcurrency = (delta: number) => {
    const next = Math.max(
      0,
      Math.min(queue.concurrency.max_allowed, queue.concurrency.target + delta),
    );
    if (next === queue.concurrency.target) return;
    patchQueue.mutate({ name: queue.name, data: { concurrency: next } });
  };

  return (
    <Card className={circuitOpen ? "border-destructive" : undefined}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base capitalize">{queue.name}</CardTitle>
          <div className="flex items-center gap-2">
            {circuitOpen && (
              <Badge variant="destructive" className="gap-1">
                <AlertTriangle className="h-3 w-3" />
                Circuit {queue.circuit.state}
              </Badge>
            )}
            <Badge variant={paused ? "outline" : "secondary"}>
              {paused ? "Paused" : "Running"}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {orderedDepth(queue.depth).map(([status, count]) => (
            <Badge key={status} variant={STATUS_BADGE[status] ?? "outline"}>
              {count} {status}
            </Badge>
          ))}
          {Object.keys(queue.depth).length === 0 && (
            <span className="text-sm text-muted-foreground">Empty</span>
          )}
        </div>

        {circuitOpen && (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
            {queue.circuit.reason ?? "Suspended"}
            {queue.circuit.retry_after && (
              <> — retrying after {formatFullDate(queue.circuit.retry_after)}</>
            )}
          </div>
        )}

        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">Concurrency</span>
          <div className="flex items-center gap-1.5">
            <Button
              variant="outline"
              size="icon-sm"
              disabled={queue.concurrency.target <= 0 || patchQueue.isPending}
              onClick={() => setConcurrency(-1)}
              title="Decrease concurrency"
              aria-label="Decrease concurrency"
            >
              <Minus className="h-3 w-3" />
            </Button>
            <span className="w-16 text-center tabular-nums">
              {queue.concurrency.actual}/{queue.concurrency.target}
              <span className="text-muted-foreground"> (max {queue.concurrency.max_allowed})</span>
            </span>
            <Button
              variant="outline"
              size="icon-sm"
              disabled={
                queue.concurrency.target >= queue.concurrency.max_allowed ||
                patchQueue.isPending
              }
              onClick={() => setConcurrency(1)}
              title="Increase concurrency"
              aria-label="Increase concurrency"
            >
              <Plus className="h-3 w-3" />
            </Button>
          </div>
        </div>

        <div className="flex justify-end">
          <Button
            variant="outline"
            size="sm"
            disabled={patchQueue.isPending}
            onClick={() =>
              patchQueue.mutate({
                name: queue.name,
                data: { state: paused ? "running" : "paused" },
              })
            }
          >
            {paused ? (
              <>
                <Play className="mr-1 h-3 w-3" />
                Resume
              </>
            ) : (
              <>
                <Pause className="mr-1 h-3 w-3" />
                Pause
              </>
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
