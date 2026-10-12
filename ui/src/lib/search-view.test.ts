import { test } from "node:test";
import assert from "node:assert/strict";
import { chooseSearchView, type Fetched } from "./search-view.ts";

const KEY = "q|all|";
const textHits: Fetched<string> = { key: KEY, status: "success", items: ["text-1", "text-2"] };
const pending = (key = KEY): Fetched<string> => ({ key, status: "pending" });

test("without semantic mode the text result is the view", () => {
  const view = chooseSearchView({ semantic: false, currentKey: KEY, text: textHits, meaning: pending() });
  assert.deepEqual(view.items, ["text-1", "text-2"]);
  assert.equal(view.muted, false);
  assert.equal(view.notice, null);
});

test("text results show at once, muted, while the meaning search runs", () => {
  const view = chooseSearchView({ semantic: true, currentKey: KEY, text: textHits, meaning: pending() });
  assert.deepEqual(view.items, ["text-1", "text-2"]);
  assert.equal(view.source, "text");
  assert.equal(view.muted, true);
  assert.equal(view.notice, "searching");
  assert.equal(view.loading, false);
});

test("nothing is shown yet while neither has answered", () => {
  const view = chooseSearchView({ semantic: true, currentKey: KEY, text: pending(), meaning: pending() });
  assert.equal(view.loading, true);
  assert.deepEqual(view.items, []);
});

test("the tiered list replaces the text results when it lands", () => {
  const meaning: Fetched<string> = { key: KEY, status: "success", items: ["tier-1", "tier-2"] };
  const view = chooseSearchView({ semantic: true, currentKey: KEY, text: textHits, meaning });
  assert.deepEqual(view.items, ["tier-1", "tier-2"]);
  assert.equal(view.source, "semantic");
  assert.equal(view.muted, false);
  assert.equal(view.notice, null);
});

test("an empty tiered result still replaces the text results", () => {
  const meaning: Fetched<string> = { key: KEY, status: "success", items: [] };
  const view = chooseSearchView({ semantic: true, currentKey: KEY, text: textHits, meaning });
  assert.deepEqual(view.items, []);
  assert.equal(view.source, "semantic");
});

test("a 503 keeps the text list, no longer muted, and says semantic search is unavailable", () => {
  const meaning: Fetched<string> = { key: KEY, status: "error", errorStatus: 503 };
  const view = chooseSearchView({ semantic: true, currentKey: KEY, text: textHits, meaning });
  assert.deepEqual(view.items, ["text-1", "text-2"]);
  assert.equal(view.muted, false);
  assert.equal(view.notice, "unavailable");
  assert.equal(view.error, null);
});

test("a 503 before the text results have arrived waits for them", () => {
  const meaning: Fetched<string> = { key: KEY, status: "error", errorStatus: 503 };
  const view = chooseSearchView({ semantic: true, currentKey: KEY, text: pending(), meaning });
  assert.equal(view.loading, true);
  assert.equal(view.notice, "unavailable");
});

test("any other failure is an error, not a fallback", () => {
  const meaning: Fetched<string> = { key: KEY, status: "error", errorStatus: 500, errorMessage: "boom" };
  const view = chooseSearchView({ semantic: true, currentKey: KEY, text: textHits, meaning });
  assert.equal(view.error, "boom");
  assert.deepEqual(view.items, []);
});

test("a response to an earlier query is ignored", () => {
  const stale: Fetched<string> = { key: "older|all|", status: "success", items: ["old-tier-1"] };
  const view = chooseSearchView({ semantic: true, currentKey: KEY, text: textHits, meaning: stale });
  assert.deepEqual(view.items, ["text-1", "text-2"]);
  assert.equal(view.muted, true);
  assert.equal(view.notice, "searching");

  const staleText: Fetched<string> = { key: "older|all|", status: "success", items: ["old-text"] };
  const loading = chooseSearchView({ semantic: true, currentKey: KEY, text: staleText, meaning: pending() });
  assert.equal(loading.loading, true);
});

test("a 503 for an earlier query does not mark the current one unavailable", () => {
  const stale: Fetched<string> = { key: "older|all|", status: "error", errorStatus: 503 };
  const view = chooseSearchView({ semantic: true, currentKey: KEY, text: textHits, meaning: stale });
  assert.equal(view.notice, "searching");
});
