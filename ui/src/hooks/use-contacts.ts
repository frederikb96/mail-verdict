/** TanStack Query hooks for contacts and address books. */

import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { ContactCreateRequest, ContactSearchHit, ContactUpdateRequest } from "@/types/api";

export const contactKeys = {
  list: (addressbookId?: string, q?: string) =>
    ["contacts", addressbookId ?? "all", q ?? ""] as const,
  detail: (id: string) => ["contact", id] as const,
};

export function useAddressbooks() {
  return useQuery({
    queryKey: ["addressbooks"],
    queryFn: () => api.addressbooks.list(),
    staleTime: 60_000,
  });
}

export function useContacts(addressbookId?: string, q?: string) {
  return useInfiniteQuery({
    queryKey: contactKeys.list(addressbookId, q),
    queryFn: ({ pageParam }) =>
      api.contacts.list({
        addressbook_id: addressbookId,
        q: q || undefined,
        cursor: pageParam ?? undefined,
        limit: 100,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : undefined),
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
}

export function useContact(id: string | null) {
  return useQuery({
    queryKey: contactKeys.detail(id ?? ""),
    queryFn: () => api.contacts.get(id!),
    enabled: !!id,
    staleTime: 60_000,
  });
}

/** Debounced, abortable search for the compose recipient autocomplete. The
 * server does the filtering (`filter={null}` on the combobox); this hook
 * only owns the debounce and cancellation of the in-flight request. */
export function useContactSearch(query: string, debounceMs = 200) {
  const [results, setResults] = useState<ContactSearchHit[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!query.trim()) {
      setResults([]);
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    const timer = setTimeout(() => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      api
        .contacts.search(query)
        .then((hits) => {
          if (!controller.signal.aborted) setResults(hits);
        })
        .catch(() => {
          if (!controller.signal.aborted) setResults([]);
        })
        .finally(() => {
          if (!controller.signal.aborted) setIsLoading(false);
        });
    }, debounceMs);
    return () => {
      clearTimeout(timer);
      controllerRef.current?.abort();
    };
  }, [query, debounceMs]);

  return { results, isLoading };
}

export function useCreateContact() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: ContactCreateRequest) => api.contacts.create(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["contacts"] }),
  });
}

export function useUpdateContact() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: ContactUpdateRequest }) =>
      api.contacts.update(id, data),
    onSuccess: (_result, { id }) => {
      qc.invalidateQueries({ queryKey: ["contacts"] });
      qc.invalidateQueries({ queryKey: contactKeys.detail(id) });
    },
  });
}

export function useDeleteContact() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.contacts.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["contacts"] }),
  });
}
