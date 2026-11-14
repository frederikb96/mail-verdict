import { test } from "node:test";
import assert from "node:assert/strict";

import { signInLabel } from "./sign-ins.ts";
import type { SignIn } from "../types/api.ts";

function signIn(overrides: Partial<SignIn>): SignIn {
  return {
    id: "1", user_id: "u", kind: "phone", name: null, subject: "sub-1",
    first_seen_at: "2026-01-01T00:00:00Z", last_seen_at: "2026-01-02T00:00:00Z",
    revoked_at: null, current: false, ...overrides,
  };
}

test("a phone is its own name, else just a phone", () => {
  assert.equal(signInLabel(signIn({ name: "Kim's Pixel" })), "Kim's Pixel");
  assert.equal(signInLabel(signIn({})), "Phone");
});

test("a machine client without a name is its subject", () => {
  assert.equal(signInLabel(signIn({ kind: "machine" })), "sub-1");
  assert.equal(signInLabel(signIn({ kind: "machine", name: "backup script" })), "backup script");
});

test("a person's browsers are one entry whatever they are called", () => {
  assert.equal(signInLabel(signIn({ kind: "browser", name: "ignored" })), "Web browsers");
});
