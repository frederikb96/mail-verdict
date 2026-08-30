/**
 * SSE client hook for real-time updates from the backend.
 *
 * Connects to /api/events, handles reconnect with Last-Event-ID,
 * updates Jotai connection state and invalidates TanStack Query cache.
 */

"use client";

import { useEffect, useRef } from "react";
import { useSetAtom } from "jotai";
import { useQueryClient } from "@tanstack/react-query";
import { sseConnectionStateAtom } from "@/store/connection-atom";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import { useToast } from "@/hooks/use-toast";
import type { OutboxStatus, SSEEvent } from "@/types/api";

const RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_DELAY_MS = 30000;

const OUTBOX_TOAST: Record<OutboxStatus, { message: string; variant: "success" | "warning" | "error" } | null> = {
  queued: null,
  sending: null,
  sent: { message: "Message sent", variant: "success" },
  failed: { message: "Sending failed, retrying", variant: "warning" },
  dead: {
    message: "Could not send message — check SMTP settings on this account",
    variant: "error",
  },
};

export function useSSE(accountId?: string) {
  const setConnectionState = useSetAtom(sseConnectionStateAtom);
  const queryClient = useQueryClient();
  const { push: pushToast } = useToast();
  const lastEventIdRef = useRef<string | null>(null);
  const reconnectDelayRef = useRef(RECONNECT_DELAY_MS);
  const sourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    function connect() {
      // Clean up previous
      if (sourceRef.current) {
        sourceRef.current.close();
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }

      let url = "/api/events";
      const params = new URLSearchParams();
      if (accountId) {
        params.set("account_id", accountId);
      }
      if (lastEventIdRef.current) {
        params.set("last_event_id", lastEventIdRef.current);
      }
      const paramStr = params.toString();
      if (paramStr) {
        url += `?${paramStr}`;
      }

      const source = new EventSource(url);
      sourceRef.current = source;

      source.onopen = () => {
        reconnectDelayRef.current = RECONNECT_DELAY_MS;
        setConnectionState("connected");
      };

      source.onerror = () => {
        source.close();
        sourceRef.current = null;
        setConnectionState("reconnecting");
        // Schedule reconnect with exponential backoff
        reconnectTimerRef.current = setTimeout(() => {
          connect();
        }, reconnectDelayRef.current);
        reconnectDelayRef.current = Math.min(
          reconnectDelayRef.current * 2,
          MAX_RECONNECT_DELAY_MS,
        );
      };

      // A reconnect gap loses NOTIFYs in between; the server tells us to
      // invalidate everything once rather than trust a stale cache.
      source.addEventListener("resync", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries();
      });

      source.addEventListener("mail.new", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          queryClient.invalidateQueries({ queryKey: ["mails"] });
          if (data.folder_id) {
            invalidateAllFolderCaches(queryClient);
          }
        } catch {
          // Ignore
        }
      });

      source.addEventListener("mail.updated", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          if (data.message_id) {
            queryClient.invalidateQueries({
              queryKey: ["mail", data.message_id],
            });
          }
          queryClient.invalidateQueries({ queryKey: ["mails"] });
          invalidateAllFolderCaches(queryClient);
        } catch {
          // Ignore
        }
      });

      source.addEventListener("mail.deleted", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["mails"] });
        queryClient.invalidateQueries({ queryKey: ["unified"] });
        queryClient.invalidateQueries({ queryKey: ["folders"] });
      });

      source.addEventListener("verdict.issued", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          if (data.message_id) {
            queryClient.invalidateQueries({
              queryKey: ["mail", data.message_id],
            });
          }
        } catch {
          // Ignore
        }
      });

      // Account state events
      source.addEventListener("account.changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["accounts"] });
        queryClient.invalidateQueries({ queryKey: ["sync-status"] });
      });

      // A folder finished syncing (including initial backfill) — refetch
      // its message list and counts.
      source.addEventListener("folder.synced", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["mails"] });
        invalidateAllFolderCaches(queryClient);
      });

      source.addEventListener("folder.changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        invalidateAllFolderCaches(queryClient);
      });

      // Send/draft status: toast + refresh the outbox list.
      source.addEventListener("outbox.updated", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          queryClient.invalidateQueries({ queryKey: ["outbox"] });
          if (data.status) {
            const toast = OUTBOX_TOAST[data.status];
            if (toast) {
              pushToast(toast.message, toast.variant, data.status === "dead" ? 0 : 5000);
            }
          }
          if (data.status === "sent") {
            // The sent copy lands in the account's Sent folder on its next sync.
            queryClient.invalidateQueries({ queryKey: ["mails"] });
            invalidateAllFolderCaches(queryClient);
          }
        } catch {
          // Ignore
        }
      });
    }

    connect();

    return () => {
      if (sourceRef.current) {
        sourceRef.current.close();
        sourceRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      setConnectionState("disconnected");
    };
  }, [accountId, setConnectionState, queryClient, pushToast]);
}
