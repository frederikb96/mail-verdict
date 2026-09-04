/** TanStack Query hooks for contacts and address books. */

import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useAtom } from "jotai";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { selectedContactIdAtom } from "@/lib/atoms";
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

/** The open contact lives in the URL (`?id=...`), the way an open message
 * does -- linkable and survives the back button. The URL is the source of
 * truth: this hook mirrors it into `selectedContactIdAtom` on every
 * navigation (including back/forward), and `selectContact` is the one way
 * to change it. A caller must not write the atom directly, or the two
 * fall out of sync the next time the URL changes. Needs a Suspense
 * boundary above it (see app/contacts/page.tsx) -- `useSearchParams()`
 * requires one under this app's static export. */
export function useContactSelection() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [selectedId, setSelectedId] = useAtom(selectedContactIdAtom);

  useEffect(() => {
    setSelectedId(searchParams.get("id"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const selectContact = useCallback(
    (id: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (id) params.set("id", id);
      else params.delete("id");
      const qs = params.toString();
      router.push(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
    },
    [router, pathname, searchParams],
  );

  return { selectedId, selectContact };
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
