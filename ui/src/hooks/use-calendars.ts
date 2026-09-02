/** TanStack Query hooks for calendars and the identity-to-calendar mapping. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { CalendarCreateRequest, CalendarLinksUpdate, CalendarUpdateRequest } from "@/types/api";

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
    onSuccess: () => qc.invalidateQueries({ queryKey: calendarKeys.list }),
  });
}

export function useDeleteCalendar() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.calendars.delete(id),
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
