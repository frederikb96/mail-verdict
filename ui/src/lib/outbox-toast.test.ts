import { test } from "node:test";
import assert from "node:assert/strict";
import { outboxToastFor } from "./outbox-toast.ts";

test("a send that succeeds says it was sent", () => {
  const toast = outboxToastFor("sent", "send");
  assert.equal(toast?.message, "Message sent");
  assert.equal(toast?.variant, "success");
});

test("a send that dead-letters blames SMTP", () => {
  const toast = outboxToastFor("dead", "send");
  assert.match(toast?.message ?? "", /SMTP/);
  assert.equal(toast?.variant, "error");
});

test("a restore that succeeds never says 'sent'", () => {
  const toast = outboxToastFor("sent", "append");
  assert.equal(toast?.message, "Message restored");
  assert.equal(toast?.variant, "success");
  assert.doesNotMatch(toast?.message ?? "", /sent/i);
});

test("a restore that dead-letters never blames SMTP -- it never opened one", () => {
  const toast = outboxToastFor("dead", "append");
  assert.doesNotMatch(toast?.message ?? "", /SMTP/i);
  assert.equal(toast?.variant, "error");
});

test("a restore still in progress shows nothing, the same as a send", () => {
  assert.equal(outboxToastFor("pending", "append"), null);
  assert.equal(outboxToastFor("processing", "append"), null);
});

test("an undefined kind (a draft, or a payload from an older server) falls back to send wording", () => {
  const toast = outboxToastFor("sent", undefined);
  assert.equal(toast?.message, "Message sent");
});
