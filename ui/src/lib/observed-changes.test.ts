import { test } from "node:test";
import assert from "node:assert/strict";
import { getObservedChanges, recordObservedChange } from "./observed-changes.ts";

test("a replayed move and its move back leave only where the message ended up", () => {
  recordObservedChange("m1", "inbox", "work");
  recordObservedChange("m1", "work", "inbox");
  recordObservedChange("m2", "inbox", null);
  const changes = getObservedChanges();
  assert.deepEqual(
    changes.map((c) => [c.messages[0].id, c.targetFolderId ?? null]),
    [["m1", "inbox"], ["m2", null]],
  );
});
