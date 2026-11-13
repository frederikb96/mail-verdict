import { test } from "node:test";
import assert from "node:assert/strict";
import {
  actionError,
  buildEffects,
  buildWhen,
  conditionError,
  emptyAction,
  emptyConditionRow,
  parseEffects,
  parseWhen,
} from "./rule-form.ts";

function roundTripWhen(when: unknown) {
  const form = parseWhen(when);
  assert.notEqual(form, null, JSON.stringify(when));
  return buildWhen(form!);
}

test("conditions the form shows come back unchanged", () => {
  const trees = [
    { sender_match: "a@b.c" },
    { all: [{ subject_contains: "sale" }, { not: { sender_domain: "shop.example" } }] },
    { any: [{ header_match: { field: "From", pattern: "bob" } }, { header_exists: "List-Id" }] },
    { all: [{ size_gt: 2 * 1024 * 1024 }, { size_lt: 300 * 1024 }, { has_attachment: true }] },
    { all: [{ has_attachment: "pdf" }, { any: [{ folder_is: "INBOX" }, { tag_is: "work" }] }] },
    { all: [{ verdict_is: "spam" }, { verdict_is: "not-spam" }] },
    { all: [{ body_matches: "^x+$" }, { subject_matches: "(a|b)" }, { body_contains: "q" }] },
  ];
  for (const tree of trees) assert.deepEqual(roundTripWhen(tree), tree);
});

test("an empty or single-entry tree normalises to the same meaning", () => {
  assert.deepEqual(roundTripWhen({}), {});
  assert.deepEqual(roundTripWhen({ all: [{ tag_is: "a" }] }), { tag_is: "a" });
  assert.deepEqual(roundTripWhen({ not: { tag_is: "a" } }), { not: { tag_is: "a" } });
});

test("a tree the form cannot show exactly is refused, not trimmed", () => {
  const unshowable = [
    { moon_phase: "full" },
    { all: [{ subject_contains: "a", sender_domain: "b" }] },
    { all: [{ all: [{ any: [{ tag_is: "a" }] }] }] },
    { not: { all: [{ tag_is: "a" }] } },
    { size_gt: 1500 },
    { has_attachment: false },
    { verdict_is: "maybe" },
    { header_match: { field: "x", pattern: "y", flags: "i" } },
  ];
  for (const tree of unshowable) assert.equal(parseWhen(tree), null, JSON.stringify(tree));
});

test("effects round-trip, and flags split into one effect each", () => {
  const effects = [
    { move: { special_use: "junk" } },
    { move: { folder_name: "Receipts" } },
    { move: { folder_id: "0b1f1c2e-0000-4000-8000-000000000001" } },
    { set_flags: { seen: true } },
    { set_flags: { flagged: false } },
    { keywords: { add: ["k"] } },
    { tag: { add: ["t"] } },
    { trash: {} },
    { expunge: {} },
    { notify: { text: "hello" } },
  ];
  assert.deepEqual(buildEffects(parseEffects(effects)!), effects);
  assert.deepEqual(buildEffects(parseEffects([{ set_flags: { seen: true, flagged: true } }])!), [
    { set_flags: { seen: true } },
    { set_flags: { flagged: true } },
  ]);
});

test("effects the form does not offer are refused", () => {
  for (const effect of [
    { webhook: { name: "w", url: "https://x" } },
    { record_verdict: { is_spam: true } },
    { enqueue_order: {} },
    { set_flags: { answered: true } },
    { keywords: { remove: ["k"] } },
    { move: { folder_name: "A", special_use: "junk" } },
  ]) {
    assert.equal(parseEffects([effect]), null, JSON.stringify(effect));
  }
});

test("rows say what is missing", () => {
  assert.equal(conditionError({ ...emptyConditionRow("subject_contains"), value: "" }), "Enter a value");
  assert.equal(conditionError({ ...emptyConditionRow("subject_matches"), value: "(" }) !== null, true);
  assert.equal(conditionError({ ...emptyConditionRow("subject_matches"), value: "a+" }), null);
  assert.equal(conditionError({ ...emptyConditionRow("header_match"), value: "x" }), "Name the header");
  assert.equal(conditionError({ ...emptyConditionRow("size_gt"), value: "abc" }), "Enter a size");
  assert.equal(conditionError(emptyConditionRow("attachment_any")), null);
  assert.equal(actionError(emptyAction("move")), "Choose a folder");
  assert.equal(actionError({ ...emptyAction("move"), value: "use:junk" }), null);
  assert.equal(actionError(emptyAction("trash")), null);
});
