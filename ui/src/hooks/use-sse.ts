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
import { syncStatesAtom } from "@/lib/atoms";
import type { SSEEvent } from "@/types/api";

const RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_DELAY_MS = 30000;

export function useSSE(accountId?: string) {
  const setSyncStates = useSetAtom(syncStatesAtom);
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
      if (accountId) {
        url += `?account_id=${accountId}`;
      }

      const source = new EventSource(url);
      sourceRef.current = source;

      source.onopen = () => {
        reconnectDelayRef.current = RECONNECT_DELAY_MS;
      };

      source.onerror = () => {
        source.close();
        sourceRef.current = null;
        // Schedule reconnect with exponential backoff
        reconnectTimerRef.current = setTimeout(() => {
          connect();
        }, reconnectDelayRef.current);
        reconnectDelayRef.current = Math.min(
          reconnectDelayRef.current * 2,
          MAX_RECONNECT_DELAY_MS,
        );
      };

      // Sync state events
      source.addEventListener("sync.state", (e: MessageEvent) => {
        try {
          lastEventIdRef.current = e.lastEventId;
          const data: SSEEvent = JSON.parse(e.data);
          if (data.account_id) {
            setSyncStates((prev) => ({
              ...prev,
              [data.account_id!]: {
                status: data.status ?? "unknown",
                can_sync: data.can_sync ?? false,
                can_cancel: data.can_cancel ?? false,
                current_folder: data.current_folder,
                folder_index: data.folder_index,
                folder_total: data.folder_total,
                synced: data.synced,
                total_messages: data.total_messages,
                new_mails: data.new_mails,
                errors: data.errors,
                duration_s: data.duration_s,
                error_message: data.error_message,
              },
            }));
          }
        } catch {
          // Ignore parse errors
        }
      });

      // Mail events - invalidate queries
      source.addEventListener("mail.new", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          queryClient.invalidateQueries({ queryKey: ["mails"] });
          if (data.folder_id) {
            queryClient.invalidateQueries({ queryKey: ["folders"] });
          }
        } catch {
          // Ignore
        }
      });

      source.addEventListener("mail.updated", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          if (data.mail_id) {
            queryClient.invalidateQueries({
              queryKey: ["mail", data.mail_id],
            });
          }
          queryClient.invalidateQueries({ queryKey: ["mails"] });
        } catch {
          // Ignore
        }
      });

      source.addEventListener("mail.deleted", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["mails"] });
        queryClient.invalidateQueries({ queryKey: ["folders"] });
      });

      source.addEventListener("verdict.issued", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          if (data.mail_id) {
            queryClient.invalidateQueries({
              queryKey: ["mail", data.mail_id],
            });
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
    };
  }, [accountId, setSyncStates, queryClient]);
}
