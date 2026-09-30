import { test } from "node:test";
import assert from "node:assert/strict";
import { stableOrder } from "./stable-order.ts";

interface Item {
  id: string;
  label: string;
}

test("at the top, fresh replaces shown outright and nothing is held", () => {
  const shown: Item[] = [{ id: "a", label: "old a" }];
  const fresh: Item[] = [
    { id: "new", label: "new" },
    { id: "a", label: "fresh a" },
  ];
  const result = stableOrder(shown, fresh, true);
  assert.deepEqual(result.rows, fresh);
  assert.deepEqual(result.held, []);
});

test("scrolled down, a row already shown keeps its position and refreshes its content", () => {
  const shown: Item[] = [
    { id: "a", label: "old a" },
    { id: "b", label: "old b" },
  ];
  const fresh: Item[] = [
    { id: "b", label: "new b" },
    { id: "a", label: "new a" },
  ];
  const result = stableOrder(shown, fresh, false);
  // Position from `shown` (a, then b), content from `fresh`.
  assert.deepEqual(result.rows, [
    { id: "a", label: "new a" },
    { id: "b", label: "new b" },
  ]);
  assert.deepEqual(result.held, []);
});

test("scrolled down, an order new to the list is held back rather than inserted", () => {
  const shown: Item[] = [{ id: "a", label: "a" }];
  const fresh: Item[] = [
    { id: "new", label: "new order" },
    { id: "a", label: "a" },
  ];
  const result = stableOrder(shown, fresh, false);
  assert.deepEqual(result.rows, [{ id: "a", label: "a" }]);
  assert.deepEqual(result.held, [{ id: "new", label: "new order" }]);
});

test("scrolled down, an order gone from fresh (deleted or merged away) leaves the shown rows", () => {
  const shown: Item[] = [
    { id: "a", label: "a" },
    { id: "b", label: "b" },
  ];
  const fresh: Item[] = [{ id: "a", label: "a" }];
  const result = stableOrder(shown, fresh, false);
  assert.deepEqual(result.rows, [{ id: "a", label: "a" }]);
  assert.deepEqual(result.held, []);
});

test("an empty shown list holds everything fresh brings, scrolled down", () => {
  const result = stableOrder<Item>([], [{ id: "a", label: "a" }], false);
  assert.deepEqual(result.rows, []);
  assert.deepEqual(result.held, [{ id: "a", label: "a" }]);
});
