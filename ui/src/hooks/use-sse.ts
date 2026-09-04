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
import {
  invalidateMailListsBounded,
  mailKeys,
  refreshMailFromServer,
  removeMailFromAllListCaches,
  removeMailFromCache,
} from "@/hooks/use-mails";
import { useToast } from "@/hooks/use-toast";
import type { OutboxStatus, SSEEvent } from "@/types/api";

const RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_DELAY_MS = 30000;

const OUTBOX_TOAST: Record<OutboxStatus, { message: string; variant: "success" | "warning" | "error" } | null> = {
  pending: null,
  processing: null,
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
          // A truly new row's data isn't in the event -- only a fetch gets
          // it, and a shallowly-loaded list gets one immediately; a deeply
          // scrolled one is marked stale and catches up on its own later,
          // rather than this firing hundreds of page refetches at once.
          invalidateMailListsBounded(queryClient);
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
          // Message events carry the row's id as `id`, not `message_id`
          // (that name is verdict.issued's own convention).
          if (data.id) {
            queryClient.invalidateQueries({ queryKey: mailKeys.thread(data.id) });
            if (data.changed?.includes("folder_id")) {
              // Moved out of whatever folder cache held it; the folder it
              // moved into (if currently viewed) catches up via the bounded
              // refetch below rather than a fetch reconstructing the row.
              removeMailFromCache(queryClient, data.id);
              invalidateMailListsBounded(queryClient);
            } else {
              // Only the changed columns' new names are on the event, not
              // their values -- one bounded fetch of this row patches both
              // its detail cache and every list row for it, never a full
              // list refetch.
              void refreshMailFromServer(queryClient, data.id);
            }
          }
          invalidateAllFolderCaches(queryClient);
        } catch {
          // Ignore
        }
      });

      source.addEventListener("mail.deleted", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          if (data.id) {
            removeMailFromAllListCaches(queryClient, data.id);
          } else {
            invalidateMailListsBounded(queryClient);
          }
        } catch {
          invalidateMailListsBounded(queryClient);
        }
        queryClient.invalidateQueries({ queryKey: ["unified", "folders"] });
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

      // A write PostIMAP gave up on permanently -- refresh the notification
      // centre's unread count and list.
      source.addEventListener("notification.new", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["notifications"] });
      });

      // Account state events
      source.addEventListener("account.changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["accounts"] });
        queryClient.invalidateQueries({ queryKey: ["sync-status"] });
      });

      // A folder finished syncing (including initial backfill) — a full
      // refetch is the right cost here (unlike mail.new/mail.updated,
      // this fires once per sync pass, not once per message, and a
      // resync can shift page contents arbitrarily) so this stays a
      // plain invalidate rather than the bounded helper above.
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
          if (data.itip === "reply") {
            // A calendar reply's outbox row -- own_reply on the event and
            // the invitation card both read its status, not the mail toast.
            queryClient.invalidateQueries({ queryKey: ["calendar-events"] });
            queryClient.invalidateQueries({ queryKey: ["calendar-event"] });
            queryClient.invalidateQueries({ queryKey: ["invitation"] });
          } else if (data.status) {
            const toast = OUTBOX_TOAST[data.status as OutboxStatus];
            if (toast) {
              pushToast(toast.message, toast.variant, data.status === "dead" ? 0 : 5000);
            }
          }
          if (data.status === "sent" && data.itip !== "reply") {
            // The sent copy lands in the account's Sent folder on its next sync.
            queryClient.invalidateQueries({ queryKey: ["mails"] });
            invalidateAllFolderCaches(queryClient);
          }
        } catch {
          // Ignore
        }
      });

      // One line per finished run -- the pipeline live tail and failures
      // table refresh from this rather than polling alone.
      source.addEventListener("pipeline.run_finished", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["runs"] });
        queryClient.invalidateQueries({ queryKey: ["queues"] });
      });

      // Calendar sync is polling-based (60s) on the backend, so this only
      // refreshes what a poll already changed -- it doesn't shorten the lag.
      source.addEventListener("calendar.object", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["calendar-events"], refetchType: "active" });
        queryClient.invalidateQueries({ queryKey: ["calendar-event"] });
        queryClient.invalidateQueries({ queryKey: ["invitation"] });
      });

      source.addEventListener("calendar.collection", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["calendars"] });
      });

      source.addEventListener("calendar.account", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["dav-accounts"] });
      });

      source.addEventListener("contact.object", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["contacts"] });
      });

      source.addEventListener("contact.collection", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["addressbooks"] });
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
