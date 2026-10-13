/**
 * SSE client hook for real-time updates from the backend.
 *
 * Connects to /api/events, handles reconnect with Last-Event-ID,
 * updates Jotai connection state and invalidates TanStack Query cache.
 */

"use client";

import { useEffect, useRef } from "react";
import { useSetAtom } from "jotai";
import { type Query, type QueryClient, focusManager, useQueryClient } from "@tanstack/react-query";
import { sseConnectionStateAtom } from "@/store/connection-atom";
import { mailArrivedAtom } from "@/lib/atoms";
import { isMailListQuery } from "@/lib/query-persister";
import { invalidateAllFolderCaches } from "@/hooks/use-folders";
import {
  mailKeys,
  refreshMailLists,
  refreshMailViews,
  refreshThreads,
} from "@/hooks/use-mails";
import { alertKeys } from "@/hooks/use-alerts";
import { useEffectiveAlertFolderIds } from "@/hooks/use-push";
import { useToast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import { networkRecovered } from "@/lib/intent-drainer";
import { getLedgerSnapshot } from "@/lib/intent-ledger";
import {
  type BufferedMailEvent,
  bufferMailEvent,
  ownEcho,
  planMailRefresh,
  triageMailEvents,
} from "@/lib/mail-event-buffer";
import { recordObservedChange } from "@/lib/observed-changes";
import {
  type StreamActivity,
  WATCHDOG_INTERVAL_MS,
  streamIsStale,
} from "@/lib/stream-watchdog";
import { folderAlertsEnabled } from "@/lib/alert-prefs";
import { closeResolvedNotifications, showAlertNotification } from "@/lib/live-notifications";
import {
  type CalendarObjectPayload,
  resolveCalendarInvalidationTargets,
} from "@/components/calendar/calendar-sse-targets";
import { outboxToastFor } from "@/lib/outbox-toast";
import type { EventListResponse, OutboxKind, OutboxStatus, SSEEvent } from "@/types/api";

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
 * An arrival re-reads the open conversation whole (refreshThreads), since
 * mail.new names no thread -- so a sync or bulk move producing arrivals on
 * every flush would re-download it twice a second. One re-read per window,
 * the last one trailing, still catches the message that belongs to it.
 */
const THREAD_ARRIVAL_WINDOW_MS = 5000;

/** Queries refetched at once after a resync -- the rest follow in turns. */
const RESYNC_REFETCH_BATCH = 4;

/**
 * After a resync, mark every query other than the mail lists stale and
 * re-read the ones on screen a few at a time. Refetching them all at once
 * on a connection that just came back competes with the mail lists and
 * with any mail action waiting to be sent. A query nobody is showing
 * refetches when it is shown again.
 */
async function refetchStaggered(queryClient: QueryClient): Promise<void> {
  const predicate = (query: Query) => !isMailListQuery(query.queryKey);
  await queryClient.invalidateQueries({ predicate, refetchType: "none" });
  const active = queryClient.getQueryCache().findAll({ predicate, type: "active" });
  for (let i = 0; i < active.length; i += RESYNC_REFETCH_BATCH) {
    const batch = new Set(active.slice(i, i + RESYNC_REFETCH_BATCH));
    await queryClient
      .refetchQueries({ predicate: (query) => batch.has(query), type: "active" })
      .catch(() => undefined);
  }
}

export function useSSE(accountId?: string) {
  const setConnectionState = useSetAtom(sseConnectionStateAtom);
  const setMailArrived = useSetAtom(mailArrivedAtom);
  const queryClient = useQueryClient();
  const { push: pushToast } = useToast();
  const enabledFolderIds = useEffectiveAlertFolderIds();
  // Read fresh inside the SSE handler (a closure captured once at connect
  // time) without forcing a reconnect every time the preference changes --
  // effects below depend on it explicitly for that reason.
  const enabledFolderIdsRef = useRef(enabledFolderIds);
  enabledFolderIdsRef.current = enabledFolderIds;
  const lastEventIdRef = useRef<string | null>(null);
  const reconnectDelayRef = useRef(RECONNECT_DELAY_MS);
  const sourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activityRef = useRef<StreamActivity>({ lastAt: 0, pinged: false });

  // Buffered mail.new/mail.updated/mail.deleted events -- see FLUSH_INTERVAL_MS.
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingMailEventsRef = useRef<BufferedMailEvent[]>([]);
  const threadArrivalTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const threadArrivalPendingRef = useRef(false);

  // Buffered calendar.object state -- calendar sync is polling-based on the
  // backend (60s), but a single poll landing several changes still fires
  // one event per row, and every mounted month-chunk observer used to
  // invalidate on every one of them. Buffered the same way as mail above,
  // then targeted at flush time (see calendar-sse-targets.ts) rather than
  // invalidating every loaded chunk.
  const calFlushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingCalendarEventsRef = useRef<CalendarObjectPayload[]>([]);

  useEffect(() => {
    function flushPending() {
      flushTimerRef.current = null;
      // An event for a message this browser has a request out for is the echo
      // of that request, which re-reads everything it touched once answered:
      // the answered ones are dropped, the unanswered ones wait for it.
      const { apply, hold } = triageMailEvents(
        pendingMailEventsRef.current, getLedgerSnapshot().intents, Date.now(),
      );
      pendingMailEventsRef.current = hold;
      if (hold.length > 0) scheduleFlush();
      const plan = planMailRefresh(apply);

      // Rows that changed, arrived or left, and every count, are re-read
      // together by refreshMailViews -- one request per open list showing a
      // folder the events touched, however deep it is scrolled, so the list
      // and the counts never disagree.
      for (const id of plan.updatedIds) {
        queryClient.invalidateQueries({ queryKey: mailKeys.detail(id) });
      }
      refreshThreads(queryClient, new Set([...plan.updatedIds, ...plan.removedIds]), false);
      if (plan.newOrMoved) scheduleThreadArrivalRefresh();
      if (plan.folderCounts || plan.newOrMoved || plan.removedIds.size > 0 || plan.updatedIds.size > 0) {
        refreshMailViews(queryClient, plan.folders ? { folderIds: plan.folders } : null);
      }
    }

    /** Buffer a mail event for the next flush. */
    function bufferEvent(kind: BufferedMailEvent["kind"], data: SSEEvent | null) {
      pendingMailEventsRef.current.push(bufferMailEvent(kind, data, Date.now()));
      scheduleFlush();
    }

    /** Hide a message other clients moved or removed from every list at
     * once -- unless this browser's own request is what moved it, which its
     * own projection already shows. */
    function observeChange(id: string, from: string | null, to: string | null) {
      if (ownEcho(id, Date.now(), getLedgerSnapshot().intents) !== "none") return;
      recordObservedChange(id, from, to);
    }

    function scheduleFlush() {
      if (flushTimerRef.current) return;
      flushTimerRef.current = setTimeout(flushPending, FLUSH_INTERVAL_MS);
    }

    function scheduleThreadArrivalRefresh() {
      if (threadArrivalTimerRef.current) {
        threadArrivalPendingRef.current = true;
        return;
      }
      refreshThreads(queryClient, new Set(), true);
      threadArrivalTimerRef.current = setTimeout(() => {
        threadArrivalTimerRef.current = null;
        if (!threadArrivalPendingRef.current) return;
        threadArrivalPendingRef.current = false;
        scheduleThreadArrivalRefresh();
      }, THREAD_ARRIVAL_WINDOW_MS);
    }

    function flushCalendarPending() {
      calFlushTimerRef.current = null;
      const pending = pendingCalendarEventsRef.current;
      pendingCalendarEventsRef.current = [];
      if (pending.length === 0) return;

      // Which months currently hold each object -- computed once for the
      // whole flush window, from whatever chunks happen to be cached right
      // now, rather than once per event.
      const monthsByObjectId = new Map<string, string[]>();
      for (const [key, data] of queryClient.getQueriesData<EventListResponse>({
        queryKey: ["calendar-events"],
      })) {
        const month = key[1] as string | undefined;
        if (!month || !data) continue;
        for (const e of data.events) {
          const months = monthsByObjectId.get(e.object_id);
          if (months) months.push(month);
          else monthsByObjectId.set(e.object_id, [month]);
        }
      }

      const targets = new Set<string>();
      let invalidateAll = false;
      for (const payload of pending) {
        const result = resolveCalendarInvalidationTargets(
          payload, payload.id ? (monthsByObjectId.get(payload.id) ?? []) : [],
        );
        if (result === "all") {
          invalidateAll = true;
          break;
        }
        for (const month of result) targets.add(month);
      }

      if (invalidateAll) {
        queryClient.invalidateQueries({ queryKey: ["calendar-events"], refetchType: "active" });
        return;
      }

      queryClient.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "calendar-events" && targets.has(query.queryKey[1] as string),
        refetchType: "active",
      });
    }

    function scheduleCalendarFlush() {
      if (calFlushTimerRef.current) return;
      calFlushTimerRef.current = setTimeout(flushCalendarPending, FLUSH_INTERVAL_MS);
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
      activityRef.current = { lastAt: Date.now(), pinged: false };

      // Everything the server sends counts as the stream being alive.
      const listen = (type: string, handler: (e: MessageEvent) => void) =>
        source.addEventListener(type, (e) => {
          activityRef.current.lastAt = Date.now();
          handler(e as MessageEvent);
        });

      // The server's heartbeat: EventSource hides comment lines, so a quiet
      // stream is only told from a dead one by this named event.
      listen("ping", () => {
        activityRef.current.pinged = true;
      });

      source.onopen = () => {
        reconnectDelayRef.current = RECONNECT_DELAY_MS;
        setConnectionState("connected");
        // Mail actions waiting out a network backoff go now.
        networkRecovered();
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

      // The first event on a fresh connection. Its id is recorded like any
      // other's, so a connection that drops before a single real event
      // reached it still reconnects with an id the server can replay from --
      // without one, the reconnect is a fresh connection and everything
      // that happened during the gap is lost.
      listen("connected", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
      });

      // A reconnect gap loses NOTIFYs in between; the server tells us to
      // invalidate everything once rather than trust a stale cache. The mail
      // lists are refreshed as windows instead -- invalidating one would
      // re-read every page it has loaded, one request at a time.
      listen("resync", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        refreshMailLists(queryClient);
        void refetchStaggered(queryClient);
      });

      // mail.new/mail.updated/mail.deleted are buffered rather than acted
      // on per event: a bulk write over a whole folder fires one of these
      // per row (thousands, for a large folder), all in a burst far
      // tighter than handling even one of them takes -- see
      // FLUSH_INTERVAL_MS. Each handler below only records into the
      // pending buffer and schedules a flush; flushPending is what
      // actually touches the cache.
      listen("mail.new", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        let data: SSEEvent | null = null;
        try {
          data = JSON.parse(e.data);
        } catch {
          // Buffered as unreadable: every open list is re-read.
        }
        if (data?.id && data.account_id && data.folder_id) {
          setMailArrived({ accountId: data.account_id, folderId: data.folder_id, messageId: data.id });
        }
        bufferEvent("new", data);
      });

      listen("mail.updated", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        let data: SSEEvent | null = null;
        try {
          data = JSON.parse(e.data);
        } catch {
          // Buffered as unreadable: every open list is re-read.
        }
        // Message events carry the row's id as `id`, not `message_id`
        // (that name is verdict.issued's own convention). The conversations
        // holding it are refreshed at flush time, with the rest of the burst
        // (refreshThreads). A move names both ends (old_folder_id), so the
        // flush re-reads the lists showing either; the row is gone from its
        // old folder's lists at once, not a re-read later.
        if (data?.id && data.folder_id && data.changed?.includes("folder_id")) {
          observeChange(data.id, data.old_folder_id ?? null, data.folder_id);
        }
        bufferEvent("updated", data);
      });

      // invalidateAllFolderCaches (folders, folder-order, and unified
      // together) already covers a delete's counts on the batched flush --
      // no separate immediate call needed.
      listen("mail.deleted", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        let data: SSEEvent | null = null;
        try {
          data = JSON.parse(e.data);
        } catch {
          // Buffered as unreadable: every open list is re-read.
        }
        if (data?.id) observeChange(data.id, data.folder_id ?? null, null);
        bufferEvent("deleted", data);
      });

      listen("verdict.issued", (e: MessageEvent) => {
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
            refreshThreads(queryClient, new Set([data.message_id]), false);
          }
        } catch {
          // Ignore
        }
      });

      // A new alert (currently: new mail) -- refresh the bell's own list
      // and count, and raise a system notification when this browser
      // has been granted permission and this folder is one of
      // the ones allowed to alert (see alert-prefs.ts). Not gated on the
      // tab being focused: an unfocused tab is exactly the case a
      // notification exists for, and a focused one simply gets both the
      // popup and the already-visible bell update.
      listen("alert.new", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: alertKeys.list });
        queryClient.invalidateQueries({ queryKey: alertKeys.count });
        try {
          const data: SSEEvent = JSON.parse(e.data);
          const allowed = folderAlertsEnabled(enabledFolderIdsRef.current, data.folder_id);
          if (
            allowed &&
            typeof window !== "undefined" &&
            "Notification" in window &&
            Notification.permission === "granted"
          ) {
            void showAlertNotification({
              id: data.id,
              title: data.title || "New mail",
              body: data.body ?? undefined,
              url: data.url ?? undefined,
            });
          }
        } catch {
          // Ignore -- the invalidate above already refreshed the bell.
        }
      });

      // Withdraws an alert everywhere it is still showing -- dismissed on
      // one browser or device, cleared on every other open one too. The
      // event itself carries no id (see alerts/resolve.py), so a system
      // notification already displayed is closed by reading back which
      // alerts are still undismissed and closing whatever no longer
      // belongs to that set, rather than trying to parse one out of here.
      listen("alert.dismissed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: alertKeys.list });
        queryClient.invalidateQueries({ queryKey: alertKeys.count });
        api.alerts
          .list(200, null, false)
          .then((alerts) => {
            const liveIds = new Set(
              alerts.filter((a) => a.dismissed_at === null).map((a) => a.id),
            );
            void closeResolvedNotifications(liveIds);
          })
          .catch(() => {
            // The list refetch above (via the invalidate) is what the bell
            // itself depends on; a displayed notification simply stays
            // until the reader dismisses it by hand this time.
          });
      });

      // A write PostIMAP gave up on permanently -- refresh the notification
      // centre's unread count and list.
      listen("notification.new", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["notifications"] });
        // The bell's badge counts write failures too (useBellBadge).
        queryClient.invalidateQueries({ queryKey: alertKeys.count });
      });

      // Account state events
      listen("account.changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["accounts"] });
        queryClient.invalidateQueries({ queryKey: ["account-order"] });
        queryClient.invalidateQueries({ queryKey: ["sync-status"] });
      });

      // A folder finished syncing (including initial backfill) -- a resync
      // can change any row, which is exactly what a window refresh re-reads.
      listen("folder.synced", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        let folderId: string | undefined;
        try {
          folderId = (JSON.parse(e.data) as SSEEvent).folder_id ?? undefined;
        } catch {
          folderId = undefined;
        }
        refreshMailViews(queryClient, folderId ? { folderIds: new Set([folderId]) } : null);
      });

      listen("folder.changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        invalidateAllFolderCaches(queryClient);
      });

      // Send/draft status: toast + refresh the outbox list.
      listen("outbox.updated", (e: MessageEvent) => {
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
          } else if (data.status && data.kind !== "draft") {
            // A draft append also reaches status "sent" -- that's PostIMAP
            // confirming the draft copy landed in the Drafts folder, not a
            // send, so it gets none of these toasts. compose-form already
            // told the user "Draft saved" when the row was created.
            const toast = outboxToastFor(
              data.status as OutboxStatus, data.kind as OutboxKind | undefined,
            );
            if (toast) {
              pushToast(toast.message, toast.variant, data.status === "dead" ? 0 : 5000);
            }
          }
          if (data.status === "sent" && data.itip !== "reply") {
            // The sent (or drafts) copy lands in the account's folder on its next sync.
            refreshMailViews(queryClient);
          }
        } catch {
          // Ignore
        }
      });

      // One line per finished run -- the pipeline live tail and failures
      // table refresh from this rather than polling alone.
      listen("pipeline.run_finished", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["runs"] });
        queryClient.invalidateQueries({ queryKey: ["queues"] });
      });

      // Calendar sync is polling-based (60s) on the backend, so this only
      // refreshes what a poll already changed -- it doesn't shorten the lag.
      listen("calendar.object", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          pendingCalendarEventsRef.current.push({
            id: data.id, dtstart: data.dtstart, dtend: data.dtend, is_recurring: data.is_recurring,
          });
        } catch {
          // An unparseable payload can't be targeted -- resolved as "all"
          // by flushCalendarPending, which treats a missing id the same
          // way (empty months-containing-id, no dtstart/dtend either).
          pendingCalendarEventsRef.current.push({});
        }
        // The detail and invitation caches stay immediate: each targets
        // one specific cached entry and only costs a real fetch if that
        // exact one happens to be open, so unlike the chunk list above it
        // never fans out into a storm.
        queryClient.invalidateQueries({ queryKey: ["calendar-event"] });
        queryClient.invalidateQueries({ queryKey: ["invitation"] });
        scheduleCalendarFlush();
      });

      listen("calendar.collection", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["calendars"] });
      });

      // The identity-to-calendar mapping is one document for the whole
      // instance, replaced wholesale, and a viewer holds the base_revision
      // its own next save is checked against -- so a second browser that
      // missed this would have its save refused rather than merged.
      listen("calendar.links_changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["calendar-links"] });
      });

      listen("calendar.account", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["dav-accounts"] });
      });

      listen("contact.object", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["contacts"] });
      });

      listen("contact.collection", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["addressbooks"] });
      });

      // Settings are MailVerdict's own table -- nothing upstream announces
      // a write to it, so a category changed in another tab reaches this
      // one only via this event. Invalidating the bare ["settings"] prefix
      // covers both the all-categories query and the single-category one.
      listen("settings.changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["settings"] });
        // bell_badge_counts_new_mail decides what the server badge counts.
        queryClient.invalidateQueries({ queryKey: alertKeys.count });
      });

      // Orders is a cross-account register with no account-scoped event of
      // its own to ride -- broadcast to every ring (see api/events.py's
      // broadcast_event). Invalidates the list (one request for the whole
      // loaded window) and the one order's own detail, when open.
      listen("order.updated", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        try {
          const data: SSEEvent = JSON.parse(e.data);
          queryClient.invalidateQueries({ queryKey: ["orders"] });
          if (data.order_id) {
            queryClient.invalidateQueries({ queryKey: ["orders", "detail", data.order_id] });
          }
        } catch {
          // Ignore
        }
      });

      // Identities are MailVerdict's own table too -- the compose "from"
      // selector, reply/forward and RSVP all read the same list.
      listen("identity.changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        queryClient.invalidateQueries({ queryKey: ["identities"] });
      });

      // The pipeline document's own optimistic-concurrency check
      // (base_revision) exists because more than one editor is expected at
      // once -- an agent and the UI, most notably -- so a second viewer
      // needs the current revision pushed to it rather than finding out
      // only when its own next save gets a 409.
      listen("pipeline.document_changed", (e: MessageEvent) => {
        lastEventIdRef.current = e.lastEventId;
        // Every pipeline query key is nested under this one prefix
        // (document, health, revisions, stage-types), so one invalidate
        // covers the lot -- a document change can move all three of the
        // first, and the fourth never changes at runtime anyway.
        queryClient.invalidateQueries({ queryKey: ["pipeline"] });
      });
    }

    /** Open a fresh stream now instead of waiting out a backoff or a dead
     * socket; the last event id replays what was missed, or answers resync. */
    function reconnectNow() {
      reconnectDelayRef.current = RECONNECT_DELAY_MS;
      setConnectionState("reconnecting");
      connect();
    }

    function reconnectIfStale() {
      if (sourceRef.current && streamIsStale(activityRef.current, Date.now())) reconnectNow();
    }

    // A network change leaves the old socket unusable without the browser
    // necessarily noticing; a page coming back into view after a sleep finds
    // the same, or sits in a backoff that no longer applies.
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (sourceRef.current) reconnectIfStale();
      else reconnectNow();
    };
    const watchdog = setInterval(reconnectIfStale, WATCHDOG_INTERVAL_MS);
    window.addEventListener("online", reconnectNow);
    document.addEventListener("visibilitychange", onVisible);

    connect();

    return () => {
      clearInterval(watchdog);
      window.removeEventListener("online", reconnectNow);
      document.removeEventListener("visibilitychange", onVisible);
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
      pendingMailEventsRef.current = [];
      if (calFlushTimerRef.current) {
        clearTimeout(calFlushTimerRef.current);
        calFlushTimerRef.current = null;
      }
      if (threadArrivalTimerRef.current) {
        clearTimeout(threadArrivalTimerRef.current);
        threadArrivalTimerRef.current = null;
      }
      threadArrivalPendingRef.current = false;
      setConnectionState("disconnected");
    };
  }, [accountId, setConnectionState, setMailArrived, queryClient, pushToast]);

  // The mail lists opt out of TanStack's own refetch on focus, which would
  // re-read every loaded page one request at a time; a window regaining
  // focus re-reads each open list as one window instead. The counts refetch
  // on focus through their own queries.
  useEffect(
    () =>
      focusManager.subscribe((focused) => {
        if (focused) refreshMailLists(queryClient);
      }),
    [queryClient],
  );
}
