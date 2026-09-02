/**
 * TanStack Query hooks for calendar events.
 *
 * Events are fetched by calendar-month chunk (`["calendar-events", "2026-09"]`)
 * rather than by view range -- the month scroller, the time grid and the
 * agenda all read from the same chunks, so a chunk fetched once for the
 * month view is not re-fetched for the day view landing on the same week.
 */

import {
  type QueryClient,
  keepPreviousData,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import { monthChunkKey } from "@/lib/dates";
import type {
  EventCreateRequest,
  EventDeleteRequest,
  EventInstance,
  EventListResponse,
  EventUpdateRequest,
  RespondRequest,
} from "@/types/api";

export const eventKeys = {
  chunk: (month: string) => ["calendar-events", month] as const,
};

/** A stable key for one instance within a chunk -- a modified occurrence of
 * a recurring series shares its object_id with the master, so recurrence_id
 * has to be part of the identity. */
function instanceKey(e: Pick<EventInstance, "object_id" | "recurrence_id">): string {
  return `${e.object_id}:${e.recurrence_id ?? "master"}`;
}

export function useEventChunk(month: string) {
  return useQuery({
    queryKey: eventKeys.chunk(month),
    queryFn: () => api.events.list({ month }),
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  });
}

/** Every month chunk touching [from, to], merged and filtered to the range. */
export function useEventsForRange(from: Date, to: Date) {
  const months = monthsBetween(from, to);
  const results = useQueries({
    queries: months.map((month) => ({
      queryKey: eventKeys.chunk(month),
      queryFn: () => api.events.list({ month }),
      staleTime: 5 * 60_000,
      placeholderData: keepPreviousData,
    })),
  });

  const isLoading = results.some((r) => r.isLoading);
  const byKey = new Map<string, EventInstance>();
  for (const r of results) {
    for (const e of r.data?.events ?? []) {
      byKey.set(instanceKey(e), e);
    }
  }
  const fromMs = from.getTime();
  const toMs = to.getTime();
  const events = Array.from(byKey.values()).filter((e) => {
    const start = new Date(e.dtstart).getTime();
    const end = new Date(e.dtend).getTime();
    return end >= fromMs && start <= toMs;
  });

  return { events, isLoading };
}

function monthsBetween(from: Date, to: Date): string[] {
  const months: string[] = [];
  const cursor = new Date(from.getFullYear(), from.getMonth(), 1);
  const end = new Date(to.getFullYear(), to.getMonth(), 1);
  while (cursor <= end) {
    months.push(monthChunkKey(cursor));
    cursor.setMonth(cursor.getMonth() + 1);
  }
  return months;
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

    onSettled: () => qc.invalidateQueries({ queryKey: ["calendar-events"] }),
  });
}

export function useDeleteEvent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ objectId, data }: { objectId: string; data?: EventDeleteRequest }) =>
      api.events.delete(objectId, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["calendar-events"] }),
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

    onSettled: () => qc.invalidateQueries({ queryKey: ["calendar-events"] }),
  });
}
