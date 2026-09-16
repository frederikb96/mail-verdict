import { test } from "node:test";
import assert from "node:assert/strict";
import { QueryClient, QueriesObserver } from "@tanstack/query-core";
import { identity } from "./query-combine.ts";

/**
 * useEventsForRange/useWeekEvents (use-events.ts) depend on `results`
 * (the array useQueries() returns) as a single useMemo deps entry --
 * correct only if that array is itself referentially stable across a
 * render where nothing about the underlying queries changed. This
 * exercises query-core directly (QueryClient/QueriesObserver, no React,
 * no bundler) rather than mounting the hook -- there is no React test
 * renderer in this project, and the mechanism under test lives entirely
 * in query-core's own `combine` handling, not in React.
 */
test("useQueries results stay referentially stable across calls when nothing changed, given identity as combine", () => {
  const client = new QueryClient();
  const queryKey = ["use-events-stability-test", "2026-09"];
  // Seeded directly, with an infinite staleTime, rather than letting a
  // queryFn actually fetch -- this test is about the RESULT ARRAY's own
  // reference, not about resolving a real query, and seeding gives an
  // already-`success` query from the first read with nothing async to
  // wait on.
  client.setQueryData(queryKey, { events: [], truncated: false });

  const queryOptions = [
    {
      queryKey,
      queryFn: () => Promise.resolve({ events: [], truncated: false }),
      staleTime: Number.POSITIVE_INFINITY,
    },
  ];

  const observer = new QueriesObserver(client, queryOptions, { combine: identity });

  const [, getCombinedResultFirst] = observer.getOptimisticResult(queryOptions, identity);
  const first = getCombinedResultFirst();

  const [, getCombinedResultSecond] = observer.getOptimisticResult(queryOptions, identity);
  const second = getCombinedResultSecond();

  assert.equal(
    first, second,
    "the combined results array should be the SAME reference across two calls with nothing changed",
  );

  observer.destroy();
  client.clear();
});

test("without a combine, query-core rebuilds the array fresh every call -- the failure mode this guards against", () => {
  const client = new QueryClient();
  const queryKey = ["use-events-stability-test-nocombine", "2026-09"];
  client.setQueryData(queryKey, { events: [], truncated: false });

  const queryOptions = [
    {
      queryKey,
      queryFn: () => Promise.resolve({ events: [], truncated: false }),
      staleTime: Number.POSITIVE_INFINITY,
    },
  ];

  const observer = new QueriesObserver(client, queryOptions);

  const [first] = observer.getOptimisticResult(queryOptions, undefined);
  const [second] = observer.getOptimisticResult(queryOptions, undefined);

  assert.notEqual(
    first, second,
    "this documents the library's own default behaviour -- if this ever starts passing, " +
      "the `combine: identity` workaround above may no longer be necessary",
  );

  observer.destroy();
  client.clear();
});
