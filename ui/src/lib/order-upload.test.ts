import { test } from "node:test";
import assert from "node:assert/strict";
import { ACCEPTED_UPLOADS, isHeic, jpegName, waitForFile } from "./order-upload.ts";
import type { OrderFileStatus } from "@/types/api";

const status = (s: OrderFileStatus["status"], extra: Partial<OrderFileStatus> = {}): OrderFileStatus => ({
  file_id: "f",
  order_id: s === "attached" ? "o" : null,
  status: s,
  last_error: null,
  ...extra,
});

test("heic and heif are recognised by type or by name", () => {
  assert.equal(isHeic({ name: "IMG_1.HEIC", type: "" }), true);
  assert.equal(isHeic({ name: "photo", type: "image/heif" }), true);
  assert.equal(isHeic({ name: "photo.jpg", type: "image/jpeg" }), false);
});

test("the converted name swaps the extension", () => {
  assert.equal(jpegName("IMG_1.HEIC"), "IMG_1.jpg");
  assert.equal(jpegName("no-extension"), "no-extension.jpg");
});

test("the picker offers what the server accepts, and no heic", () => {
  assert.match(ACCEPTED_UPLOADS, /\.pdf/);
  assert.match(ACCEPTED_UPLOADS, /\.pkpass/);
  assert.doesNotMatch(ACCEPTED_UPLOADS, /hei/i);
});

test("waiting returns the first settled answer", async () => {
  const answers = [status("pending"), status("pending"), status("attached")];
  let calls = 0;
  const result = await waitForFile(async () => answers[calls++], {
    intervalMs: 1,
    timeoutMs: 1000,
    sleep: async () => {},
  });
  assert.equal(result.status, "attached");
  assert.equal(calls, 3);
});

test("a failed file is returned with its error", async () => {
  const result = await waitForFile(async () => status("failed", { last_error: "unreadable" }), {
    intervalMs: 1,
    timeoutMs: 1000,
    sleep: async () => {},
  });
  assert.equal(result.status, "failed");
  assert.equal(result.last_error, "unreadable");
});

test("a file that stays pending is handed back as pending at the timeout", async () => {
  let elapsed = 0;
  const result = await waitForFile(async () => status("pending"), {
    intervalMs: 10,
    timeoutMs: 35,
    sleep: async (ms) => {
      elapsed += ms;
    },
  });
  assert.equal(result.status, "pending");
  assert.ok(elapsed >= 30 && elapsed <= 40);
});
