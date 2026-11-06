import { test } from "node:test";
import assert from "node:assert/strict";
import { PERSIST_MAX_AGE, shouldPersistQuery } from "./query-persister.ts";

const now = Date.UTC(2026, 10, 6, 12, 0);
const query = (queryKey: unknown[], ageMs: number, status = "success") => ({
  queryKey,
  state: { status, dataUpdatedAt: now - ageMs },
});

test("a recently fetched query is persisted", () => {
  assert.equal(shouldPersistQuery(query(["settings", "providers"], 60_000), now), true);
});

test("a query whose data is older than the max age is dropped, however often the cache is rewritten", () => {
  assert.equal(shouldPersistQuery(query(["settings", "providers"], PERSIST_MAX_AGE + 1), now), false);
});

test("failed and ephemeral queries are not persisted", () => {
  assert.equal(shouldPersistQuery(query(["settings"], 0, "error"), now), false);
  assert.equal(shouldPersistQuery(query(["calendar-events", "2026-11"], 0), now), false);
  assert.equal(shouldPersistQuery(query(["mails", "a", "b"], 0), now), false);
});
