import { test } from "node:test";
import assert from "node:assert/strict";
import { QueryClient, QueryObserver } from "@tanstack/query-core";
import { withFields } from "./query-result.ts";

/**
 * Exercises query-core's own field tracking (QueryObserver.trackResult,
 * what useQuery returns) rather than a stand-in, since the point is which
 * fields it records as read.
 */
function trackedResult() {
  const client = new QueryClient();
  const observer = new QueryObserver(client, {
    queryKey: ["query-result-test"],
    queryFn: () => Promise.resolve([1, 2, 3]),
    enabled: false,
  });
  const read = new Set<string | symbol>();
  const result = observer.trackResult(observer.getCurrentResult(), (key) => read.add(key));
  return { result, read };
}

test("reading one field through the view tracks only that field", () => {
  const { result, read } = trackedResult();
  const view = withFields(result, { data: ["projected"] });
  assert.equal(view.isPending, true);
  assert.deepEqual([...read], ["isPending"]);
});

test("replaced and added fields come from the given values", () => {
  const { result, read } = trackedResult();
  const view = withFields(result, { data: ["projected"], readAt: 42 });
  assert.deepEqual(view.data, ["projected"]);
  assert.equal(view.readAt, 42);
  assert.equal(read.size, 0);
});

test("spreading the view still carries every field, replaced ones included", () => {
  const { result } = trackedResult();
  const spread = { ...withFields(result, { data: ["projected"], readAt: 42 }) };
  assert.deepEqual(spread.data, ["projected"]);
  assert.equal(spread.readAt, 42);
  assert.equal(spread.fetchStatus, "idle");
  assert.equal("readAt" in withFields(result, { readAt: 42 }), true);
});

test("a plain spread reads every field -- the re-render the view avoids", () => {
  const { result, read } = trackedResult();
  const spread = { ...result, data: ["projected"] };
  assert.ok(spread);
  assert.ok(read.has("fetchStatus") && read.has("isFetching"));
});
