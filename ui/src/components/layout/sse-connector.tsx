"use client";

import { useMailIntentOutcomes } from "@/hooks/use-mail-intents";
import { useSSE } from "@/hooks/use-sse";

/** Invisible component that maintains the SSE connection and sends the
 * mail actions waiting in the intent ledger. */
export function SSEConnector() {
  useSSE();
  useMailIntentOutcomes();
  return null;
}
