"use client";

/**
 * One queue as a compact tile: its name and state, what is waiting in it,
 * and how many workers run. Changing anything (pause, worker count, resetting
 * a tripped circuit) sits behind the tile's menu, away from a stray tap.
 */

import { AlertTriangle, MoreVertical } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { formatFullDate } from "@/lib/format";
import { usePatchQueue } from "@/hooks/use-queues";
import type { QueueResponse } from "@/types/api";

const STATUS_ORDER = ["pending", "claimed", "failed", "done", "skipped", "cancelled"];
/** Statuses worth a chip; finished work is history, not state. */
const SHOWN = new Set(["pending", "claimed", "failed"]);

function orderedDepth(depth: Record<string, number>): [string, number][] {
  const known = STATUS_ORDER.filter((s) => s in depth && SHOWN.has(s)).map(
    (s) => [s, depth[s]] as [string, number],
  );
  const rest = Object.entries(depth).filter(([k]) => !STATUS_ORDER.includes(k));
  return [...known, ...rest];
}

export function QueueCard({ queue }: { queue: QueueResponse }) {
  const patchQueue = usePatchQueue();
  const circuitOpen = queue.circuit.state !== "closed";
  const paused = queue.state === "paused";
  const { target, max_allowed: max } = queue.concurrency;

  const setConcurrency = (delta: number) => {
    const next = Math.max(0, Math.min(max, target + delta));
    if (next === target) return;
    patchQueue.mutate({ name: queue.name, data: { concurrency: next } });
  };

  const depth = orderedDepth(queue.depth);
  const failed = queue.depth.failed ?? 0;

  return (
    <Card
      data-testid="queue-tile"
      size="sm"
      className={circuitOpen ? "border border-destructive" : undefined}
    >
      <div className="flex flex-col gap-2 px-3">
        <div className="flex items-center justify-between gap-2">
          <span className="min-w-0 break-words text-sm font-medium capitalize">{queue.name}</span>
          <div className="flex shrink-0 items-center gap-1">
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Controls for the ${queue.name} queue`}
                  />
                }
              >
                <MoreVertical className="h-4 w-4" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-44">
                <DropdownMenuItem
                  disabled={patchQueue.isPending}
                  onClick={() =>
                    patchQueue.mutate({
                      name: queue.name,
                      data: { state: paused ? "running" : "paused" },
                    })
                  }
                >
                  {paused ? "Resume" : "Pause"}
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  disabled={target >= max || patchQueue.isPending}
                  onClick={() => setConcurrency(1)}
                >
                  More workers
                </DropdownMenuItem>
                <DropdownMenuItem
                  disabled={target <= 0 || patchQueue.isPending}
                  onClick={() => setConcurrency(-1)}
                >
                  Fewer workers
                </DropdownMenuItem>
                {circuitOpen && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem
                      disabled={patchQueue.isPending}
                      onClick={() =>
                        patchQueue.mutate({ name: queue.name, data: { reset_circuit: true } })
                      }
                    >
                      Retry now
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant={paused ? "outline" : "secondary"}>{paused ? "Paused" : "Running"}</Badge>
          {depth.map(([status, count]) =>
            status === "failed" && count > 0 ? (
              <a key={status} href="#failures" aria-label={`${count} failed, show failures`}>
                <Badge variant="destructive">{count} failed</Badge>
              </a>
            ) : (
              <Badge key={status} variant="outline">
                {count} {status}
              </Badge>
            ),
          )}
          {depth.length === 0 && failed === 0 && (
            <span className="text-xs text-muted-foreground">empty</span>
          )}
        </div>

        <div className="text-xs text-muted-foreground">
          Workers{" "}
          <span className="tabular-nums text-foreground">
            {queue.concurrency.actual}/{target}
          </span>{" "}
          (max {max})
        </div>

        {circuitOpen && (
          <div className="flex items-start gap-1.5 rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
            <span className="min-w-0 break-words">
              Circuit {queue.circuit.state}: {queue.circuit.reason ?? "suspended"}
              {queue.circuit.retry_after && (
                <>, retrying after {formatFullDate(queue.circuit.retry_after)}</>
              )}
            </span>
          </div>
        )}
      </div>
    </Card>
  );
}
