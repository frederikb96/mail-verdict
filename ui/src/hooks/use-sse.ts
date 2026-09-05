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
} from "@/hooks/use-mails";
import { useToast } from "@/hooks/use-toast";
import type { OutboxStatus, SSEEvent } from "@/types/api";

const RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_DELAY_MS = 30000;

/**
 * A bulk write over a whole folder fires one postimap_events NOTIFY per
 * row -- thousands for a large folder, all arriving over SSE in a burst
 * far tighter than any one of them takes to handle. mail.new/mail.updated/
 * mail.deleted are collected into this buffer instead of acting per
 * event, and flushed at most once per FLUSH_INTERVAL_MS: a fixed-window
 * throttle rather than a debounce, since a debounce that keeps resetting
 * on every new event in a continuous flood would never fire until the
 * whole burst finished, leaving the interface looking frozen for however
 * long the write takes.
 */
const FLUSH_INTERVAL_MS = 500;

/**
 * Below this many distinct changed messages in one flush window, patch
 * each individually (one bounded fetch per row, via refreshMailFromServer)
 * -- the ordinary single- or few-message-action case. At or above it,
 * fetching one row at a time *is* the storm; treat the whole window as a
 * bulk change and do one bounded list refresh instead.
 */
const PATCH_BURST_THRESHOLD = 20;

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

  // Buffered mail.new/mail.updated/mail.deleted state -- see FLUSH_INTERVAL_MS.
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingNewOrMovedRef = useRef(false);
  const pendingUpdatedIdsRef = useRef<Set<string>>(new Set());
  const pendingRemovedIdsRef = useRef<Set<string>>(new Set());
  const pendingFolderCountsRef = useRef(false);

  useEffect(() => {
    function flushPending() {
      flushTimerRef.current = null;
      const removed = pendingRemovedIdsRef.current;
      const updated = pendingUpdatedIdsRef.current;
      const hadNewOrMoved = pendingNewOrMovedRef.current;
      const hadFolderCounts = pendingFolderCountsRef.current;
      pendingRemovedIdsRef.current = new Set();
      pendingUpdatedIdsRef.current = new Set();
      pendingNewOrMovedRef.current = false;
      pendingFolderCountsRef.current = false;

      for (const id of removed) removeMailFromAllListCaches(queryClient, id);

      if (updated.size > 0) {
        if (updated.size < PATCH_BURST_THRESHOLD) {
          for (const id of updated) void refreshMailFromServer(queryClient, id);
        } else {
          // A burst this wide is a bulk action, not a handful of edits --
          // fetching one row at a time here would itself be the storm.
          invalidateMailListsBounded(queryClient);
        }
      }

      if (hadNewOrMoved) invalidateMailListsBounded(queryClient);
      if (hadFolderCounts || hadNewOrMoved || removed.size > 0 || updated.size > 0) {
        invalidateAllFolderCaches(queryClient);
      }
    }

    function scheduleFlush() {
      if (flushTimerRef.current) return;
      flushTimerRef.current = setTimeout(flushPending, FLUSH_INTERVAL_MS);
    }

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

      // mail.new/mail.updated/mail.deleted are buffered rather than acted
      // on per event: a bulk write over a whole folder fires one of these
      // per row (thousands, for a large folder), all in a burst far
      // tighter than handling even one of them takes -- see
      // FLUSH_INTERVAL_MS. Each handler below only records into the
      // pending buffer and schedules a flush; flushPending is what
      // actually touches the cache.
      source.addEventListener("mail.new", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          pendingNewOrMovedRef.current = true;
          if (data.folder_id) pendingFolderCountsRef.current = true;
        } catch {
          // Ignore
        }
        scheduleFlush();
      });

      source.addEventListener("mail.updated", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          // Message events carry the row's id as `id`, not `message_id`
          // (that name is verdict.issued's own convention). Thread
          // invalidation stays immediate -- it targets one specific
          // cached query key and only ever costs a real fetch if that
          // exact thread happens to be the one open, so it never fans out
          // into the kind of storm the list/detail paths below guard
          // against.
          if (data.id) {
            queryClient.invalidateQueries({ queryKey: mailKeys.thread(data.id) });
            if (data.changed?.includes("folder_id")) {
              // Moved out of whatever folder cache held it; the folder it
              // moved into (if currently viewed) catches up via the
              // bounded refresh the flush issues, rather than a fetch
              // reconstructing the row.
              pendingRemovedIdsRef.current.add(data.id);
              pendingNewOrMovedRef.current = true;
            } else {
              pendingUpdatedIdsRef.current.add(data.id);
            }
          }
          pendingFolderCountsRef.current = true;
        } catch {
          // Ignore
        }
        scheduleFlush();
      });

      source.addEventListener("mail.deleted", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          if (data.id) {
            pendingRemovedIdsRef.current.add(data.id);
          } else {
            pendingNewOrMovedRef.current = true;
          }
        } catch {
          pendingNewOrMovedRef.current = true;
        }
        // invalidateAllFolderCaches (folders, folder-order, and unified
        // together) already covers this on the batched flush below --
        // no separate immediate call needed.
        pendingFolderCountsRef.current = true;
        scheduleFlush();
      });

      source.addEventListener("verdict.issued", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          if (data.message_id) {
            // The single-message detail cache and the reading pane's own
            // thread cache both carry a copy of the verdict -- the header's
            // thumb-up/thumb-down reads from the latter, so missing it here
            // is the same stale-header shape mark-read/unread already had.
            queryClient.invalidateQueries({
              queryKey: ["mail", data.message_id],
            });
            queryClient.invalidateQueries({
              queryKey: ["thread", data.message_id],
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
      if (flushTimerRef.current) {
        clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
      }
      setConnectionState("disconnected");
    };
  }, [accountId, setConnectionState, queryClient, pushToast]);
}
