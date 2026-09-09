/** TanStack Query hooks for calendars and the identity-to-calendar mapping. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  Calendar, CalendarCreateRequest, CalendarLinksUpdate, CalendarUpdateRequest,
} from "@/types/api";

export const calendarKeys = {
  list: ["calendars"] as const,
  links: ["calendar-links"] as const,
};

export function useCalendars() {
  return useQuery({
    queryKey: calendarKeys.list,
    queryFn: () => api.calendars.list(),
    staleTime: 60_000,
  });
}

export function useCreateCalendar() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CalendarCreateRequest) => api.calendars.create(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: calendarKeys.list }),
  });
}

export function useUpdateCalendar() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: CalendarUpdateRequest }) =>
      api.calendars.update(id, data),
    // is_visible is a client-side view concept the events endpoint never
    // filters by (each instance carries its own calendar_id, filtered
    // here) -- so toggling it needs no server round trip to feel done.
    // Applied to the cached calendars list immediately, rolled back on
    // failure. is_enabled is different: it changes what the sidebar and
    // editor even offer, and is set only from the manage dialog, not a
    // control that needs to feel instant.
    onMutate: async ({ id, data }) => {
      await qc.cancelQueries({ queryKey: calendarKeys.list });
      const previous = qc.getQueryData<Calendar[]>(calendarKeys.list);
      qc.setQueryData<Calendar[]>(
        calendarKeys.list,
        (old) => old?.map((c) => (c.id === id ? { ...c, ...data } : c)),
      );
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) qc.setQueryData(calendarKeys.list, context.previous);
    },
    onSuccess: (_calendar, { data }) => {
      qc.invalidateQueries({ queryKey: calendarKeys.list });
      // Nothing about is_visible is cached by GET /calendar/events, so
      // only an is_enabled change (or any other field) needs the month
      // view to refetch.
      if (data.is_enabled !== undefined) {
        qc.invalidateQueries({ queryKey: ["calendar-events"] });
      }
    },
  });
}

export function useDeleteCalendar() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, eventCount }: { id: string; eventCount: number }) =>
      api.calendars.delete(id, eventCount),
    onSuccess: () => qc.invalidateQueries({ queryKey: calendarKeys.list }),
  });
}

export function useCalendarLinks() {
  return useQuery({
    queryKey: calendarKeys.links,
    queryFn: () => api.calendars.links.get(),
    staleTime: 60_000,
  });
}

export function useUpdateCalendarLinks() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CalendarLinksUpdate) => api.calendars.links.update(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: calendarKeys.links }),
  });
}
