"use client";

import { useSSE } from "@/hooks/use-sse";

/** Invisible component that maintains the SSE connection. */
export function SSEConnector() {
  useSSE();
  return null;
}
