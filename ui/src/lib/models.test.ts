import { test } from "node:test";
import assert from "node:assert/strict";
import {
  effortAfterProviderChange,
  effortLevels,
  modelSuggestions,
  providerOptions,
} from "./models.ts";

test("embeddings offer no Anthropic", () => {
  assert.equal(providerOptions("embedding").some((p) => p.value === "anthropic"), false);
  assert.equal(providerOptions("chat").some((p) => p.value === "anthropic"), true);
});

test("a fake provider takes no effort and anthropic refuses none", () => {
  assert.deepEqual(effortLevels("fake"), []);
  assert.equal(effortLevels("anthropic").includes("none"), false);
  assert.equal(effortLevels("openai").includes("none"), true);
});

test("switching to anthropic replaces an effort it refuses", () => {
  assert.equal(effortAfterProviderChange("none", "anthropic"), "low");
  assert.equal(effortAfterProviderChange("high", "anthropic"), "high");
  assert.equal(effortAfterProviderChange("none", "openai"), "none");
});

test("a custom server keeps whatever effort is stored", () => {
  assert.equal(effortAfterProviderChange("minimal", "custom"), "minimal");
});

test("suggestions merge models in use without repeats or blanks", () => {
  assert.deepEqual(
    modelSuggestions("openai", "chat", ["gpt-6-luna", "claude-opus-5-5", ""]),
    ["gpt-6-luna", "claude-opus-5-5"],
  );
});
