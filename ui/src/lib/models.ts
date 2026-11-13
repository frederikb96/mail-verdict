/**
 * What the model table offers for each AI task: the providers a task may
 * use, the reasoning efforts a provider accepts, and model names worth
 * suggesting. The server validates every value; these lists only keep the
 * common choices one click away and stop an impossible pairing being typed.
 */

export type TaskKind = "chat" | "embedding";

export interface ProviderOption {
  value: string;
  label: string;
}

const CHAT_PROVIDERS: ProviderOption[] = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "custom", label: "Custom" },
  { value: "fake", label: "Fake (keywords only)" },
];

/** Anthropic has no embedding model to select. */
const EMBEDDING_PROVIDERS = CHAT_PROVIDERS.filter((p) => p.value !== "anthropic");

export function providerOptions(kind: TaskKind): ProviderOption[] {
  return kind === "embedding" ? EMBEDDING_PROVIDERS : CHAT_PROVIDERS;
}

const ANTHROPIC_EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const OPENAI_EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"];
// A custom server's accepted values are unknowable; the common ones, and
// whatever is stored, stay selectable.
const CUSTOM_EFFORTS = ["none", "low", "medium", "high"];

/** The reasoning efforts a provider accepts; empty when it takes none. */
export function effortLevels(provider: string): string[] {
  switch (provider) {
    case "anthropic":
      return ANTHROPIC_EFFORTS;
    case "openai":
      return OPENAI_EFFORTS;
    case "custom":
      return CUSTOM_EFFORTS;
    default:
      return [];
  }
}

/** The effort to keep when the provider changes: the current one if the new
 * provider accepts it, else its lowest level -- Anthropic refuses "none". */
export function effortAfterProviderChange(effort: unknown, provider: string): unknown {
  const levels = effortLevels(provider);
  if (levels.length === 0 || provider === "custom") return effort;
  return typeof effort === "string" && levels.includes(effort) ? effort : levels[0];
}

const MODEL_SUGGESTIONS: Record<TaskKind, Record<string, string[]>> = {
  chat: {
    openai: ["gpt-6-luna"],
    anthropic: ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
  },
  embedding: {
    openai: ["text-embedding-3-small", "text-embedding-3-large"],
    custom: ["Qwen/Qwen3-Embedding-8B"],
  },
};

/** The static suggestions for a provider plus models already configured
 * anywhere, without repeats. */
export function modelSuggestions(
  provider: string,
  kind: TaskKind,
  inUse: ReadonlyArray<string>,
): string[] {
  const suggestions = [...(MODEL_SUGGESTIONS[kind][provider] ?? []), ...inUse];
  return Array.from(new Set(suggestions.filter((m) => m.trim() !== "")));
}
