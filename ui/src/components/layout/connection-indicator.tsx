"use client";

import { useAtomValue } from "jotai";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { sseConnectionStateAtom, type ConnectionState } from "@/store/connection-atom";
import { cn } from "@/lib/utils";

const STATE_CONFIG: Record<ConnectionState, { color: string; label: string }> = {
  connected: { color: "bg-green-500", label: "Connected" },
  reconnecting: { color: "bg-yellow-500 animate-pulse", label: "Reconnecting..." },
  disconnected: { color: "bg-red-500", label: "Disconnected" },
};

/** Small dot indicator showing SSE connection health. */
export function ConnectionIndicator() {
  const state = useAtomValue(sseConnectionStateAtom);
  const { color, label } = STATE_CONFIG[state];

  return (
    <Tooltip>
      <TooltipTrigger
        className="flex items-center gap-1.5 px-2 py-1"
        aria-label={`Connection status: ${label}`}
      >
        <div className={cn("h-2 w-2 rounded-full", color)} />
        <span className="hidden text-xs text-muted-foreground sm:inline">
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        SSE connection: {label}
      </TooltipContent>
    </Tooltip>
  );
}
