import { test } from "node:test";
import assert from "node:assert/strict";
import { factHref, factValueText, isHttpUrl, splitFacts } from "./order-facts.ts";
import type { OrderFact } from "@/types/api";

const base: OrderFact = {
  id: "f",
  kind: "other",
  label: "Label",
  value: "Value",
  carrier: null,
  tracking_url: null,
  code: null,
  document_id: null,
};
const fact = (patch: Partial<OrderFact>): OrderFact => ({ ...base, ...patch });

test("only http and https count as links", () => {
  assert.equal(isHttpUrl("https://example.com/a?b=1"), true);
  assert.equal(isHttpUrl("http://example.com"), true);
  assert.equal(isHttpUrl("javascript:alert(1)"), false);
  assert.equal(isHttpUrl("data:text/html,x"), false);
  assert.equal(isHttpUrl("example.com"), false);
});

test("a tracking number links to the carrier page the server built", () => {
  const f = fact({ kind: "tracking_number", tracking_url: "https://track.example/1" });
  assert.equal(factHref(f), "https://track.example/1");
  assert.equal(factHref(fact({ kind: "tracking_number" })), null);
});

test("a link fact opens its own value, never a script", () => {
  assert.equal(factHref(fact({ kind: "link", value: "https://shop.example/manage" })), "https://shop.example/manage");
  assert.equal(factHref(fact({ kind: "link", value: "javascript:alert(1)" })), null);
});

test("a tracking_url that is not http is not trusted either", () => {
  assert.equal(factHref(fact({ kind: "tracking_number", tracking_url: "javascript:1" })), null);
});

test("other kinds never link", () => {
  assert.equal(factHref(fact({ kind: "place", value: "https://x.example" })), null);
});

test("dates are written out, anything else is the source's own text", () => {
  const now = new Date(2026, 9, 30);
  assert.equal(factValueText(fact({ kind: "date", value: "2026-11-02" }), now), "Mon 2 Nov");
  assert.equal(factValueText(fact({ kind: "date", value: "2 Nov, evening" }), now), "2 Nov, evening");
  assert.equal(factValueText(fact({ kind: "amount", value: "EUR 19.98" }), now), "EUR 19.98");
});

test("codes split off from the rows, order kept", () => {
  const code = {
    format: "QR",
    payload_text: "123",
    image_url: "/api/orders/o/codes/c.png",
    page: 1,
  };
  const facts = [
    fact({ id: "1", kind: "booking_code" }),
    fact({ id: "2", kind: "code", code }),
    fact({ id: "3", kind: "place" }),
    fact({ id: "4", kind: "code", code: null }),
  ];
  const { codes, rows } = splitFacts(facts);
  assert.deepEqual(codes.map((f) => f.id), ["2"]);
  assert.deepEqual(rows.map((f) => f.id), ["1", "3", "4"]);
});
