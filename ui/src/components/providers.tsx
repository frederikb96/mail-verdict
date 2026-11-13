"use client";

import { QueryClient } from "@tanstack/react-query";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { Provider as JotaiProvider } from "jotai";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useState, type ReactNode } from "react";
import { UserGate } from "@/components/user-gate";
import { meKey } from "@/hooks/use-me";
import type { MeResponse } from "@/types/api";
import {
  createQueryPersister,
  PERSIST_MAX_AGE,
  shouldPersistQuery,
} from "@/lib/query-persister";

/** 30 seconds stale time for lists, queries refetch in background after. */
const DEFAULT_STALE_TIME = 30 * 1000;

/** Nothing below renders, and nothing persisted is read, until `/api/me`
 * has said who is signed in: the stored query cache and every other piece of
 * browser state is that person's own. */
export function Providers({ children }: { children: ReactNode }) {
  return (
    <UserGate>{(me) => <SignedInProviders me={me}>{children}</SignedInProviders>}</UserGate>
  );
}

function SignedInProviders({ me, children }: { me: MeResponse; children: ReactNode }) {
  const [queryPersister] = useState(createQueryPersister);
  const [queryClient] = useState(
    () => {
      const client = new QueryClient({
        defaultOptions: {
          queries: {
            retry: 1,
            refetchOnWindowFocus: true,
            refetchOnMount: "always",
            staleTime: DEFAULT_STALE_TIME,
            gcTime: PERSIST_MAX_AGE,
          },
        },
      });
      client.setQueryData(meKey, me);
      return client;
    },
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
          // A cache written by another build is discarded: a response shape
          // that changed between releases would otherwise render from the
          // stored copy before the refetch lands, and crash the screen.
          buster: process.env.NEXT_PUBLIC_BUILD_ID,
          dehydrateOptions: {
            shouldDehydrateQuery: (query) => shouldPersistQuery(query, Date.now()),
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
