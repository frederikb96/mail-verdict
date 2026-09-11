import { test } from "node:test";
import assert from "node:assert/strict";
import { bellBadgeCount } from "./bell-badge.ts";

test("with the setting on, the badge counts mail alerts and system notifications", () => {
  assert.equal(
    bellBadgeCount({ unseenMailAlerts: 3, unacknowledgedSystem: 2, countsNewMail: true }),
    5,
  );
});

test("with the setting off, the badge counts system notifications only", () => {
  assert.equal(
    bellBadgeCount({ unseenMailAlerts: 3, unacknowledgedSystem: 2, countsNewMail: false }),
    2,
  );
  assert.equal(
    bellBadgeCount({ unseenMailAlerts: 3, unacknowledgedSystem: 0, countsNewMail: false }),
    0,
  );
});

test("before the setting has loaded, mail alerts are left out", () => {
  assert.equal(
    bellBadgeCount({ unseenMailAlerts: 3, unacknowledgedSystem: 1, countsNewMail: undefined }),
    1,
  );
});
