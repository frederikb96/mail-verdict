import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_ORDERS_VIEW,
  LEGACY_STATE_KEY,
  ORDERS_VIEW_KEY,
  parseOrdersView,
  readOrdersView,
  writeOrdersView,
} from "./order-view-state.ts";

class MemoryStorage {
  data = new Map<string, string>();
  getItem(key: string) {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.data.set(key, value);
  }
  removeItem(key: string) {
    this.data.delete(key);
  }
}

test("nothing stored is the default view", () => {
  assert.deepEqual(parseOrdersView(null, null), DEFAULT_ORDERS_VIEW);
});

test("a stored view round-trips", () => {
  const view = { kind: "ticket", open: true, favorites: false } as const;
  assert.deepEqual(parseOrdersView(JSON.stringify(view), null), view);
});

test("garbage, wrong shapes and unknown kinds fall back field by field", () => {
  assert.deepEqual(parseOrdersView("{not json", null), DEFAULT_ORDERS_VIEW);
  assert.deepEqual(parseOrdersView('"ticket"', null), DEFAULT_ORDERS_VIEW);
  assert.deepEqual(
    parseOrdersView('{"kind":"other","open":"yes","favorites":true}', null),
    { kind: "all", open: false, favorites: true },
  );
});

test("the old single-choice key migrates into the toggles", () => {
  assert.deepEqual(parseOrdersView(null, "open"), { kind: "all", open: true, favorites: false });
  assert.deepEqual(parseOrdersView(null, "favorites"), {
    kind: "all",
    open: false,
    favorites: true,
  });
  assert.deepEqual(parseOrdersView(null, "all"), DEFAULT_ORDERS_VIEW);
});

test("the new key wins over the old one", () => {
  assert.deepEqual(
    parseOrdersView('{"kind":"order","open":false,"favorites":false}', "open"),
    { kind: "order", open: false, favorites: false },
  );
});

test("writing stores the new key and drops the old one", () => {
  const storage = new MemoryStorage();
  storage.setItem(LEGACY_STATE_KEY, "open");
  writeOrdersView(storage, { kind: "ticket", open: true, favorites: true });
  assert.equal(storage.getItem(LEGACY_STATE_KEY), null);
  assert.deepEqual(readOrdersView(storage), { kind: "ticket", open: true, favorites: true });
  assert.ok(storage.getItem(ORDERS_VIEW_KEY));
});

test("a storage that throws reads as the default and writes silently", () => {
  const broken = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
    removeItem: () => {
      throw new Error("denied");
    },
  };
  assert.deepEqual(readOrdersView(broken), DEFAULT_ORDERS_VIEW);
  assert.doesNotThrow(() => writeOrdersView(broken, DEFAULT_ORDERS_VIEW));
});
