import { test } from "node:test";
import assert from "node:assert/strict";
import { parseOrderSummary } from "./order-summary.ts";

test("a plain sentence is one paragraph, one plain inline", () => {
  const blocks = parseOrderSummary("Your order shipped today.");
  assert.deepEqual(blocks, [
    { kind: "paragraph", inlines: [{ text: "Your order shipped today.", bold: false }] },
  ]);
});

test("a blank line separates two paragraphs", () => {
  const blocks = parseOrderSummary("First.\n\nSecond.");
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].kind, "paragraph");
  assert.equal(blocks[1].kind, "paragraph");
});

test("lines starting with - or * become one bullet block", () => {
  const blocks = parseOrderSummary("- item one\n- item two\n* item three");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].kind, "bullets");
  if (blocks[0].kind === "bullets") {
    assert.equal(blocks[0].items.length, 3);
    assert.equal(blocks[0].items[0][0].text, "item one");
  }
});

test("**text** becomes a bold inline, the rest stays plain", () => {
  const blocks = parseOrderSummary("Total: **EUR 49.90**, shipped.");
  assert.equal(blocks[0].kind, "paragraph");
  if (blocks[0].kind === "paragraph") {
    assert.deepEqual(blocks[0].inlines, [
      { text: "Total: ", bold: false },
      { text: "EUR 49.90", bold: true },
      { text: ", shipped.", bold: false },
    ]);
  }
});

test("no link is ever produced -- a URL and markdown-link-shaped text stay literal", () => {
  const blocks = parseOrderSummary("See https://example.com/track or [label](url) for status.");
  assert.equal(blocks[0].kind, "paragraph");
  if (blocks[0].kind === "paragraph") {
    const text = blocks[0].inlines.map((i) => i.text).join("");
    assert.match(text, /https:\/\/example\.com\/track/);
    assert.match(text, /\[label\]\(url\)/);
  }
});

test("a heading marker (#) is plain text, never rendered as a heading", () => {
  const blocks = parseOrderSummary("# Not a real heading");
  assert.equal(blocks[0].kind, "paragraph");
  if (blocks[0].kind === "paragraph") {
    assert.equal(blocks[0].inlines[0].text, "# Not a real heading");
  }
});

test("a bullet block followed by a paragraph is two separate blocks", () => {
  const blocks = parseOrderSummary("- a bullet\n\nThen a paragraph.");
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].kind, "bullets");
  assert.equal(blocks[1].kind, "paragraph");
});
