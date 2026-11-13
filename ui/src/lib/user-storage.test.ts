import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import { createStore } from "jotai";
import {
  QUERY_CACHE_KEY,
  bindUser,
  lazyAtom,
  scopedKey,
  scopedStorage,
  sweepStorage,
  unbindUser,
  userAtomWithStorage,
} from "./user-storage.ts";
import { createQueryPersister } from "./query-persister.ts";

class FakeStorage {
  private data = new Map<string, string>();
  get length() { return this.data.size; }
  key(i: number) { return [...this.data.keys()][i] ?? null; }
  getItem(k: string) { return this.data.get(k) ?? null; }
  setItem(k: string, v: string) { this.data.set(k, v); }
  removeItem(k: string) { this.data.delete(k); }
  keys() { return [...this.data.keys()].sort(); }
}

const g = globalThis as unknown as { window?: unknown };

afterEach(() => {
  unbindUser();
  delete g.window;
});

test("a scoped key before the user is known throws instead of falling back", () => {
  assert.throws(() => scopedKey("mailverdict:threaded"), /before the signed-in user/);
});

test("scoped keys carry the user id and differ between users", () => {
  bindUser("u1", new FakeStorage());
  assert.equal(scopedKey("mailverdict:threaded"), "mailverdict:threaded@u1");
  unbindUser();
  bindUser("u2", new FakeStorage());
  assert.equal(scopedKey("mailverdict:threaded"), "mailverdict:threaded@u2");
});

test("scopedStorage writes under the user's key only", () => {
  const raw = new FakeStorage();
  bindUser("u1", raw);
  scopedStorage(raw).setItem("mailverdict:x", "1");
  assert.deepEqual(raw.keys(), ["mailverdict:x@u1"]);
  unbindUser();
  bindUser("u2", raw);
  assert.equal(scopedStorage(raw).getItem("mailverdict:x"), null);
});

test("binding removes unsuffixed legacy keys and other users' query cache, and nothing else", () => {
  const raw = new FakeStorage();
  raw.setItem("theme", "dark");
  raw.setItem("mailverdict:threaded", "true");
  raw.setItem("mail-verdict-mail-intents", "{}");
  raw.setItem(`${QUERY_CACHE_KEY}@other`, "{}");
  raw.setItem(`${QUERY_CACHE_KEY}@u1`, "{}");
  raw.setItem("mailverdict:threaded@other", "false");
  bindUser("u1", raw);
  assert.deepEqual(raw.keys(), [
    `${QUERY_CACHE_KEY}@u1`,
    "mailverdict:threaded@other",
    "theme",
  ]);
});

test("sweepStorage reports what it removed", () => {
  const raw = new FakeStorage();
  raw.setItem("mv.orders.view", "x");
  assert.deepEqual(sweepStorage(raw, "u1"), ["mv.orders.view"]);
});

test("the query cache persists under the user's key", () => {
  const raw = new FakeStorage();
  g.window = { localStorage: raw };
  bindUser("u1", raw);
  const persister = createQueryPersister();
  assert.ok(persister);
  persister.persistClient({ timestamp: 1, buster: "", clientState: { mutations: [], queries: [] } });
  persister.persistClient({ timestamp: 2, buster: "", clientState: { mutations: [], queries: [] } });
  return new Promise<void>((resolve) =>
    setTimeout(() => {
      assert.ok(raw.keys().includes(`${QUERY_CACHE_KEY}@u1`));
      assert.ok(!raw.keys().includes(QUERY_CACHE_KEY));
      resolve();
    }, 1100),
  );
});

test("a user atom reads and writes the bound user's key and never another's", () => {
  const raw = new FakeStorage();
  g.window = { localStorage: raw };
  const atom = userAtomWithStorage<string>("mailverdict:q", "", { getOnInit: true });

  bindUser("u1", raw);
  raw.setItem("mailverdict:q@u1", JSON.stringify("mine"));
  raw.setItem("mailverdict:q@u2", JSON.stringify("theirs"));
  const store = createStore();
  assert.equal(store.get(atom), "mine");
  store.set(atom, (prev) => `${prev}!`);
  assert.equal(raw.getItem("mailverdict:q@u1"), JSON.stringify("mine!"));
  assert.equal(raw.getItem("mailverdict:q@u2"), JSON.stringify("theirs"));
});

test("an atom created before the user is known reads nothing at creation", () => {
  const raw = new FakeStorage();
  g.window = { localStorage: raw };
  const atom = userAtomWithStorage<string>("mailverdict:q", "", { getOnInit: true });
  const seeded = lazyAtom(() => scopedKey("mailverdict:q"));
  bindUser("u1", raw);
  const store = createStore();
  assert.equal(store.get(atom), "");
  assert.equal(store.get(seeded), "mailverdict:q@u1");
});
