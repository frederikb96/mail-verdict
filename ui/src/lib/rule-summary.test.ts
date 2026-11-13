import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CLASSIFY_SENTENCE,
  describeRule,
  ruleGroup,
  summaryText,
  usesCondition,
} from "./rule-summary.ts";
import type { StageOut } from "../types/api.ts";

function match(when: unknown, effects: unknown[], extra: Partial<StageOut> = {}): StageOut {
  return {
    stage_id: "r",
    type: "match",
    name: "r",
    config: { when, effects },
    enabled: true,
    halt: false,
    accounts: null,
    ...extra,
  };
}

const text = (stage: StageOut, folders: Record<string, string> = {}) =>
  summaryText(describeRule(stage, (id) => folders[id]));

test("every condition key reads as a phrase", () => {
  const cases: [unknown, string][] = [
    [{ sender_match: "a@b.c" }, "sender is a@b.c"],
    [{ sender_match: "b.c" }, "sender domain b.c"],
    [{ sender_domain: "b.c" }, "sender domain b.c"],
    [{ subject_contains: "sale" }, "subject contains sale"],
    [{ subject_matches: "^x" }, "subject matches /^x/"],
    [{ body_contains: "hi" }, "body contains hi"],
    [{ body_matches: "y$" }, "body matches /y$/"],
    [{ header_match: { field: "From", pattern: "bob" } }, "header From matches /bob/"],
    [{ header_exists: "List-Id" }, "header List-Id exists"],
    [{ size_gt: 2 * 1024 * 1024 }, "larger than 2 MB"],
    [{ size_lt: 300 * 1024 }, "smaller than 300 KB"],
    [{ has_attachment: true }, "has an attachment"],
    [{ has_attachment: "pdf" }, "has a pdf attachment"],
    [{ folder_is: "INBOX" }, "in INBOX"],
    [{ tag_is: "work" }, "tagged work"],
    [{ verdict_is: "spam" }, "classified as spam"],
    [{ verdict_is: "not-spam" }, "classified as not spam"],
  ];
  for (const [when, expected] of cases) {
    assert.equal(text(match(when, [{ trash: {} }])), `${expected} → move to trash`);
  }
});

test("composites join, collapse and negate", () => {
  const a = { subject_contains: "a" };
  const b = { subject_contains: "b" };
  const c = { subject_contains: "c" };
  assert.equal(
    text(match({ all: [a, b] }, [])),
    "subject contains a and subject contains b → do nothing",
  );
  assert.equal(
    text(match({ any: [a, b, c] }, [])),
    "any of: subject contains a, subject contains b (+1) → do nothing",
  );
  assert.equal(text(match({ not: a }, [])), "not subject contains a → do nothing");
  assert.equal(text(match({}, [])), "every mail → do nothing");
});

test("a long condition collapses to a count", () => {
  const long = Array.from({ length: 6 }, (_, i) => ({ subject_contains: `a-considerably-longer-phrase-number-${i}` }));
  assert.equal(text(match({ all: long }, [])), "6 conditions → do nothing");
  assert.equal(text(match({ any: long }, [])), "any of 6 conditions → do nothing");
});

test("effects resolve folders and read as verbs", () => {
  const folders = { "id-1": "Newsletter" };
  assert.equal(
    text(match({}, [{ move: { folder_id: "id-1" } }, { set_flags: { seen: true, flagged: true } }]), folders),
    "every mail → move to Newsletter, mark read, star",
  );
  assert.equal(text(match({}, [{ move: { folder_name: "Receipts" } }])), "every mail → move to Receipts");
  assert.equal(text(match({}, [{ move: { special_use: "junk" } }])), "every mail → move to Junk");
  assert.equal(
    text(
      match({}, [
        { keywords: { add: ["k"] } },
        { tag: { add: ["t"], remove: ["old"] } },
        { notify: { text: "x" } },
        { webhook: { name: "canteen" } },
        { record_verdict: { is_spam: true } },
        { enqueue_order: {} },
      ]),
    ),
    "every mail → add keyword k, add tag t, remove tag old, notify, send to canteen, record verdict, queue for orders",
  );
});

test("a rule that deletes for good says so and is flagged permanent", () => {
  const rule = match({ sender_domain: "spam.example" }, [{ expunge: {} }]);
  assert.equal(describeRule(rule).permanent, true);
  assert.match(text(rule), /delete permanently/);
  assert.equal(describeRule(match({}, [{ trash: {} }])).permanent, false);
});

test("an unknown key is shown by name, not dropped", () => {
  assert.equal(text(match({ moon_phase: "full" }, [{ teleport: {} }])), "moon_phase → teleport");
});

test("fixed stages carry their own sentences", () => {
  const classify: StageOut = { ...match({}, []), type: "classify", config: {} };
  assert.equal(describeRule(classify).when[0].text, CLASSIFY_SENTENCE);
  assert.equal(describeRule({ ...classify, halt: true }).stop, true);
});

test("groups follow what a rule does", () => {
  assert.equal(ruleGroup({ ...match({}, []), type: "classify" }), "spam");
  assert.equal(ruleGroup({ ...match({}, []), type: "orders" }), "orders");
  assert.equal(ruleGroup(match({ all: [{ verdict_is: "spam" }, { has_attachment: true }] }, [])), "spam");
  assert.equal(ruleGroup(match({ not: { verdict_is: "spam" } }, [])), "spam");
  assert.equal(ruleGroup(match({ subject_contains: "x" }, [{ webhook: { name: "w" } }])), "forwarding");
  assert.equal(ruleGroup(match({ subject_contains: "x" }, [{ trash: {} }])), "filing");
  assert.equal(ruleGroup({ ...match({}, []), type: "future" }), "other");
});

test("usesCondition walks nested trees", () => {
  assert.equal(usesCondition({ all: [{ any: [{ not: { tag_is: "a" } }] }] }, "tag_is"), true);
  assert.equal(usesCondition({ all: [{ tag_is: "a" }] }, "folder_is"), false);
});
