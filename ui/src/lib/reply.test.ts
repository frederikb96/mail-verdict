import { test } from "node:test";
import assert from "node:assert/strict";
import { buildReply } from "./reply.ts";
import type { MessageDetail } from "@/types/api";

function makeMessage(overrides: Partial<MessageDetail>): MessageDetail {
  return {
    id: "msg-1",
    account_id: "acct-1",
    folder_id: "folder-1",
    thread_id: "thread-1",
    subject: "Lunch tomorrow?",
    from_addr: "Alice <alice@example.com>",
    to_addrs: ["me@example.com"],
    received_at: "2026-09-10T12:00:00Z",
    is_seen: true,
    is_flagged: false,
    is_answered: false,
    is_draft: false,
    snippet: null,
    pending_sync: false,
    is_truncated: false,
    has_attachments: false,
    verdict_is_spam: null,
    message_id: "<msg-1@example.com>",
    cc_addrs: null,
    bcc_addrs: null,
    reply_to: null,
    in_reply_to: null,
    references: null,
    body_text: "Sure, what time?",
    body_html: null,
    size_bytes: null,
    keywords: [],
    has_blocked_images: false,
    images_allowed: false,
    created_at: "2026-09-10T12:00:00Z",
    tags: [],
    attachments: [],
    verdict: null,
    ...overrides,
  };
}

test("buildReply: with no Reply-To, a reply goes to From", () => {
  const message = makeMessage({});
  const draft = buildReply(message, "me@example.com", "reply");
  assert.deepEqual(draft.to, ["alice@example.com"]);
});

test("buildReply: a Reply-To header overrides From", () => {
  const message = makeMessage({ reply_to: "Alice Support <support@example.com>" });
  const draft = buildReply(message, "me@example.com", "reply");
  assert.deepEqual(draft.to, ["support@example.com"]);
});

test("buildReply: every address in a multi-address Reply-To is targeted", () => {
  const message = makeMessage({
    reply_to: "Bob <bob@example.com>, Carol <carol@example.com>",
  });
  const draft = buildReply(message, "me@example.com", "reply");
  assert.deepEqual(draft.to, ["bob@example.com", "carol@example.com"]);
});

test("buildReply: a comma inside a quoted display name does not split the entry", () => {
  const message = makeMessage({
    reply_to: '"Doe, Jane" <jane@example.com>, Bob <bob@example.com>',
  });
  const draft = buildReply(message, "me@example.com", "reply");
  assert.deepEqual(draft.to, ["jane@example.com", "bob@example.com"]);
});

test("buildReply: reply-all adds the original To and Cc, minus the account's own address and the new To", () => {
  const message = makeMessage({
    reply_to: "Bob <bob@example.com>",
    to_addrs: ["me@example.com", "Bob <bob@example.com>"],
    cc_addrs: ["Carol <carol@example.com>"],
  });
  const draft = buildReply(message, "me@example.com", "reply-all");
  assert.deepEqual(draft.to, ["bob@example.com"]);
  assert.deepEqual(draft.cc, ["carol@example.com"]);
});
