/**
 * SSE client hook for real-time updates from the backend.
 *
 * Connects to /api/events, handles reconnect with Last-Event-ID,
 * updates Jotai sync state atoms and invalidates TanStack Query cache.
 */

"use client";

import { useEffect, useRef } from "react";
import { useSetAtom } from "jotai";
import { useQueryClient } from "@tanstack/react-query";
import { selectedMailIdsAtom } from "@/store/selection-atom";
import { sseConnectionStateAtom } from "@/store/connection-atom";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import type { SSEEvent } from "@/types/api";

const RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_DELAY_MS = 30000;

export function useSSE(accountId?: string) {
  const setSelectedMailIds = useSetAtom(selectedMailIdsAtom);
  const setConnectionState = useSetAtom(sseConnectionStateAtom);
  const queryClient = useQueryClient();
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

      // Mail events - invalidate queries
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

      // Selection state events
      source.addEventListener("selection.changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data = JSON.parse(e.data) as {
            selected_ids: string[];
            count: number;
          };
          setSelectedMailIds(new Set(data.selected_ids));
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
  }, [accountId, setSelectedMailIds, setConnectionState, queryClient]);
}
