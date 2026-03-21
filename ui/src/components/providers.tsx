"use client";

import { QueryClient } from "@tanstack/react-query";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { Provider as JotaiProvider } from "jotai";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useState, type ReactNode } from "react";
import {
  queryPersister,
  PERSIST_MAX_AGE,
  isEphemeralQuery,
} from "@/lib/query-persister";

/** 30 seconds stale time for lists, queries refetch in background after. */
const DEFAULT_STALE_TIME = 30 * 1000;

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: 1,
            refetchOnWindowFocus: false,
            staleTime: DEFAULT_STALE_TIME,
            gcTime: PERSIST_MAX_AGE,
          },
        },
      }),
  );

  const content = (
    <JotaiProvider>
      <TooltipProvider delay={300}>{children}</TooltipProvider>
    </JotaiProvider>
  );

  if (queryPersister) {
    return (
      <PersistQueryClientProvider
        client={queryClient}
        persistOptions={{
          persister: queryPersister,
          maxAge: PERSIST_MAX_AGE,
          dehydrateOptions: {
            shouldDehydrateQuery: (query) =>
              query.state.status === "success" &&
              !isEphemeralQuery(query.queryKey),
          },
        }}
      >
        {content}
      </PersistQueryClientProvider>
    );
  }

  // SSR / non-browser fallback: no persistence
  return (
    <PersistQueryClientProvider
      client={queryClient}
      persistOptions={{
        persister: {
          persistClient: () => undefined,
          restoreClient: () => Promise.resolve(undefined),
          removeClient: () => undefined,
        },
        maxAge: PERSIST_MAX_AGE,
      }}
    >
      {content}
    </PersistQueryClientProvider>
  );
}
