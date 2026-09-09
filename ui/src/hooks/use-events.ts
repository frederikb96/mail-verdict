/**
 * TanStack Query hooks for calendar events.
 *
 * Events are fetched by calendar-month chunk (`["calendar-events", "2026-09"]`)
 * rather than by view range -- the month scroller, the time grid and the
 * agenda all read from the same chunks, so a chunk fetched once for the
 * month view is not re-fetched for the day view landing on the same week.
 */

import { useMemo } from "react";
import {
  type QueryClient,
  keepPreviousData,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useCalendars } from "@/hooks/use-calendars";
import { monthChunksForWeek, monthsBetween, weekDays } from "@/lib/dates";
import type {
  Calendar,
  EventCreateRequest,
  EventDeleteRequest,
  EventInstance,
  EventListResponse,
  EventUpdateRequest,
  RespondRequest,
} from "@/types/api";

/** The ids of every calendar the sidebar has hidden. is_visible is a
 * client-side view concept -- GET /calendar/events never filters by it,
 * which is what makes toggling it free of any server round trip -- so
 * every hook that renders events applies the same predicate, built once
 * here. A calendar_id absent from the list (a stale or still-loading
 * `calendars` query) is shown rather than hidden. */
export function hiddenCalendarIds(calendars: Calendar[] | undefined): ReadonlySet<string> {
  return new Set((calendars ?? []).filter((c) => !c.is_visible).map((c) => c.id));
}

function filterHidden(events: EventInstance[], hidden: ReadonlySet<string>): EventInstance[] {
  return hidden.size === 0 ? events : events.filter((e) => !hidden.has(e.calendar_id));
}

export const eventKeys = {
  chunk: (month: string) => ["calendar-events", month] as const,
};

/** A stable key for one instance within a chunk -- a modified occurrence of
 * a recurring series shares its object_id with the master, so recurrence_id
 * has to be part of the identity. */
function instanceKey(e: Pick<EventInstance, "object_id" | "recurrence_id">): string {
  return `${e.object_id}:${e.recurrence_id ?? "master"}`;
}

/** SSE explicitly invalidates the exact chunks a change touches (see
 * use-sse.ts's calendar.object handling), so a chunk needs no eager
 * refetch-on-mount to stay correct -- only `refetchOnMount: "always"`
 * (the app-wide default set in providers.tsx) does, and that default is
 * what turned a flick back over months already in memory into dozens of
 * redundant requests: every remounted observer refetched regardless of
 * freshness. `refetchOnMount: true` here means "refetch only if stale",
 * and the stale time is long enough that it almost never is. */
const EVENT_CHUNK_STALE_TIME = 30 * 60_000;

function chunkQueryOptions(month: string) {
  return {
    queryKey: eventKeys.chunk(month),
    queryFn: ({ signal }: { signal: AbortSignal }) => api.events.list({ month }, signal),
    staleTime: EVENT_CHUNK_STALE_TIME,
    refetchOnMount: true as const,
    placeholderData: keepPreviousData,
  };
}

export function useEventChunk(month: string) {
  return useQuery(chunkQueryOptions(month));
}

/** The full instance for the popover/editor -- fetched directly rather than
 * read from a chunk, since the chunk list may carry an abbreviated shape. */
export function useEventDetail(objectId: string | null, recurrenceId: string | null) {
  return useQuery({
    queryKey: ["calendar-event", objectId, recurrenceId] as const,
    queryFn: () => api.events.get(objectId!, recurrenceId ?? undefined),
    enabled: !!objectId,
    staleTime: 30_000,
  });
}

/** Every month chunk touching [from, to], merged and filtered to the range. */
export function useEventsForRange(from: Date, to: Date) {
  const months = monthsBetween(from, to);
  const { data: calendars } = useCalendars();
  const results = useQueries({ queries: months.map((month) => chunkQueryOptions(month)) });
  const dataRefs = results.map((r) => r.data);

  const isLoading = results.some((r) => r.isLoading);
  const fromMs = from.getTime();
  const toMs = to.getTime();

  // Referentially stable while every chunk's own `data` reference is
  // unchanged -- react-query already keeps that reference stable across
  // renders where the underlying data didn't actually change, so this
  // only recomputes on a real fetch, never on an unrelated re-render.
  const events = useMemo(() => {
    const byKey = new Map<string, EventInstance>();
    for (const r of results) {
      for (const e of r.data?.events ?? []) {
        byKey.set(instanceKey(e), e);
      }
    }
    return Array.from(byKey.values()).filter((e) => {
      const start = new Date(e.dtstart).getTime();
      const end = new Date(e.dtend).getTime();
      return end >= fromMs && start <= toMs;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, dataRefs);
  const hidden = useMemo(() => hiddenCalendarIds(calendars), [calendars]);
  const visible = useMemo(() => filterHidden(events, hidden), [events, hidden]);

  return { events: visible, isLoading };
}

/** Events touching a given week, read from whichever month chunks the week's
 * days fall into (a week can touch two, at a month boundary).
 *
 * A row only READS its chunks -- `enabled: false` here means a row never
 * starts a request of its own. Fetching belongs to the scroller's
 * useKeepEventChunksWarm observers, which follow the settled fetch window:
 * a row mounts (and this hook runs) well before its month is committed to
 * that window, and a fast flick must not fire one request per row passed.
 * Keeping the fetch decision out of the row also keeps it out of the
 * row's props, so a change of fetch window re-renders nothing here.
 *
 * `hidden` is the sidebar's hidden-calendar set, passed in rather than
 * read from the calendars query here: a few dozen mounted rows each
 * subscribing to that query would all re-render every time it so much as
 * starts a fetch, so the scroller subscribes once and hands the result
 * down.
 *
 * `loaded` is false while any relevant chunk has never had data --
 * month-week-row.tsx renders a skeleton in that case, but keeps rendering
 * events once it has ever loaded, thanks to `placeholderData:
 * keepPreviousData` keeping the previous chunk's data in place across a
 * refetch. */
export function useWeekEvents(
  weekIndex: number,
  hidden: ReadonlySet<string>,
): { events: EventInstance[]; loaded: boolean } {
  const months = monthChunksForWeek(weekIndex);
  const results = useQueries({
    queries: months.map((month) => ({ ...chunkQueryOptions(month), enabled: false })),
  });
  const dataRefs = results.map((r) => r.data);

  const days = weekDays(weekIndex);
  const weekStart = days[0].getTime();
  const weekEnd = days[6].getTime() + 24 * 60 * 60 * 1000;

  const events = useMemo(() => {
    const byKey = new Map<string, EventInstance>();
    for (const r of results) {
      for (const e of r.data?.events ?? []) {
        const start = new Date(e.dtstart).getTime();
        const end = new Date(e.dtend).getTime();
        if (end >= weekStart && start < weekEnd) byKey.set(instanceKey(e), e);
      }
    }
    return Array.from(byKey.values());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, dataRefs);
  const visible = useMemo(() => filterHidden(events, hidden), [events, hidden]);

  return { events: visible, loaded: results.every((r) => r.data !== undefined) };
}

/**
 * Keeps a durable query observer alive for every month in the given fetch
 * window, for as long as the caller (the month scroller) is mounted --
 * regardless of whether any individual week row referencing that month is
 * currently rendered. Without this, a month committed to the fetch window
 * is fetched only by the transient row that happens to trigger it, and if
 * that row scrolls back out of the render window before the request
 * resolves, react-query aborts the fetch (nothing else observes it
 * anymore) -- a month the reader deliberately paused on would then load
 * only on a second attempt. The return value is unused; this hook exists
 * purely for the fetch it anchors. */
export function useKeepEventChunksWarm(months: readonly string[]): void {
  useQueries({ queries: months.map((month) => chunkQueryOptions(month)) });
}

/** Applies `updater` to a matching instance across every loaded chunk. */
function updateEventInCache(
  qc: QueryClient,
  match: (e: EventInstance) => boolean,
  updater: (e: EventInstance) => EventInstance,
) {
  qc.setQueriesData<EventListResponse>({ queryKey: ["calendar-events"] }, (old) => {
    if (!old) return old;
    let changed = false;
    const events = old.events.map((e) => {
      if (!match(e)) return e;
      changed = true;
      return updater(e);
    });
    return changed ? { ...old, events } : old;
  });
}

export function useCreateEvent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: EventCreateRequest) => api.events.create(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["calendar-events"] }),
  });
}

interface UpdateEventVars {
  objectId: string;
  recurrenceId: string | null;
  data: EventUpdateRequest;
}

/** Move/resize/edit. Optimistically writes dtstart/dtend/summary into every
 * loaded chunk holding the instance; rolls back on error, invalidates on
 * settle so the server's own recomputation (recurrence expansion, etc.)
 * always wins eventually. */
export function useUpdateEvent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ objectId, data }: UpdateEventVars) => api.events.update(objectId, data),

    onMutate: async ({ objectId, recurrenceId, data }) => {
      await qc.cancelQueries({ queryKey: ["calendar-events"] });
      const prev = qc.getQueriesData({ queryKey: ["calendar-events"] });

      updateEventInCache(
        qc,
        (e) => e.object_id === objectId && e.recurrence_id === recurrenceId,
        (e) => ({
          ...e,
          ...(data.summary !== undefined ? { summary: data.summary } : {}),
          ...(data.dtstart !== undefined ? { dtstart: data.dtstart } : {}),
          ...(data.dtend !== undefined ? { dtend: data.dtend } : {}),
          ...(data.location !== undefined ? { location: data.location } : {}),
          ...(data.calendar_id !== undefined ? { calendar_id: data.calendar_id } : {}),
          pending: true,
        }),
      );

      return { prev };
    },

    onError: (_err, _vars, ctx) => {
      if (!ctx) return;
      for (const [key, data] of ctx.prev) qc.setQueryData(key, data);
    },

    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["calendar-events"] });
      qc.invalidateQueries({ queryKey: ["calendar-event"] });
    },
  });
}

export function useDeleteEvent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ objectId, data }: { objectId: string; data?: EventDeleteRequest }) =>
      api.events.delete(objectId, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["calendar-events"] });
      qc.invalidateQueries({ queryKey: ["calendar-event"] });
    },
  });
}

export function useRespond() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      objectId,
      recurrenceId,
      data,
    }: {
      objectId: string;
      recurrenceId: string | null;
      data: RespondRequest;
    }) => api.events.respond(objectId, data),

    onMutate: async ({ objectId, recurrenceId, data }) => {
      await qc.cancelQueries({ queryKey: ["calendar-events"] });
      const prev = qc.getQueriesData({ queryKey: ["calendar-events"] });

      // The server writes PARTSTAT immediately even when the reply itself is
      // still in flight -- reflecting that here is honest, not optimistic.
      updateEventInCache(
        qc,
        (e) => e.object_id === objectId && e.recurrence_id === recurrenceId,
        (e) => ({ ...e, partstat: data.partstat }),
      );

      return { prev };
    },

    onError: (_err, _vars, ctx) => {
      if (!ctx) return;
      for (const [key, data] of ctx.prev) qc.setQueryData(key, data);
    },

    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["calendar-events"] });
      qc.invalidateQueries({ queryKey: ["calendar-event"] });
    },
  });
}
