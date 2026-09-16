/**
 * Passed as `combine` to a useQueries() call purely for its effect on
 * TanStack's own QueriesObserver caching -- with no `combine` at all,
 * useQueries rebuilds the wrapping results array fresh on every render
 * (query-core's QueriesObserver#combineResult short-circuits to the raw,
 * newly-`.map()`-ed array when there is no combine function), so a memo
 * depending on that array recomputes every render regardless of whether
 * any query actually changed -- exactly the reference churn a
 * virtualized row's own memoization (month-week-row.tsx) is tuned
 * against. A `combine` option runs the array through query-core's
 * replaceEqualDeep, which returns the PREVIOUS array reference when
 * every element is still `===` to before -- restoring the referential
 * stability a dependent useMemo's own deps array is supposed to give.
 *
 * Module-level and argument-identical on every call (never an inline
 * arrow) is what lets QueriesObserver skip the recompute entirely once
 * nothing has changed, rather than only getting a stable return value
 * out of doing the work anyway -- see the `combine !== this.#lastCombine`
 * check in @tanstack/query-core's queriesObserver.ts.
 *
 * Kept in its own file, with no react-query import of its own, so the
 * mechanism can be unit tested directly against query-core (see
 * query-combine.test.ts) without pulling in this app's own hooks module
 * and its transitive imports -- some of which use TypeScript syntax
 * Node's own strip-only loader cannot parse.
 */
export function identity<T>(results: T): T {
  return results;
}
