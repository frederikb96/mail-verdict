import { test } from "node:test";
import assert from "node:assert/strict";
import { isMailAlertKind } from "./bell-badge.ts";

test("only the mail kind is new mail; every other kind is a system notification", () => {
  assert.equal(isMailAlertKind("mail"), true);
  assert.equal(isMailAlertKind("outbox_stalled"), false);
  assert.equal(isMailAlertKind("reminder"), false);
});
