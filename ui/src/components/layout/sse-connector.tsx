"use client";

import { useMailIntentDrainer } from "@/hooks/use-mail-intents";
import { useSSE } from "@/hooks/use-sse";

/** Invisible component that maintains the SSE connection and sends the
 * mail actions waiting in the intent ledger. */
export function SSEConnector() {
  useSSE();
  useMailIntentDrainer();
  return null;
}
