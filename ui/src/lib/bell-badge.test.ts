import { test } from "node:test";
import assert from "node:assert/strict";
import { bellBadgeCount, isMailAlertKind } from "./bell-badge.ts";

test("with the setting on, every unseen alert and every notification counts", () => {
  assert.equal(
    bellBadgeCount({
      unseenAlertsByKind: { mail: 3, outbox_stalled: 1 },
      unacknowledgedNotifications: 2,
      countsNewMail: true,
    }),
    6,
  );
});

test("with the setting off, a stalled send still counts and new mail does not", () => {
  assert.equal(
    bellBadgeCount({
      unseenAlertsByKind: { outbox_stalled: 1 },
      unacknowledgedNotifications: 0,
      countsNewMail: false,
    }),
    1,
  );
  assert.equal(
    bellBadgeCount({
      unseenAlertsByKind: { mail: 3, outbox_stalled: 1 },
      unacknowledgedNotifications: 2,
      countsNewMail: false,
    }),
    3,
  );
});

test("before the setting has loaded, only new mail is left out", () => {
  assert.equal(
    bellBadgeCount({
      unseenAlertsByKind: { mail: 3, outbox_stalled: 1 },
      unacknowledgedNotifications: 1,
      countsNewMail: undefined,
    }),
    2,
  );
});

test("only the mail kind is new mail; every other kind is a system notification", () => {
  assert.equal(isMailAlertKind("mail"), true);
  assert.equal(isMailAlertKind("outbox_stalled"), false);
  assert.equal(isMailAlertKind("reminder"), false);
});
