"use client";

/**
 * Which model does each AI task: one row per task with provider, model,
 * reasoning effort and token budget, and an Advanced fold for what is rarely
 * touched. One save bar writes every changed category.
 */

import { useId, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { CategoryHeader, SaveBar } from "@/components/settings/blocks";
import {
  NumberInput,
  OptionSelect,
  SettingFields,
  type FieldOf,
  type FieldSpec,
} from "@/components/settings/setting-fields";
import { SETTING_LABELS } from "@/components/settings/setting-labels";
import { useSettingsDraft, type SettingsDraft } from "@/hooks/use-settings-draft";
import {
  effortAfterProviderChange,
  effortLevels,
  modelSuggestions,
  providerOptions,
  type TaskKind,
} from "@/lib/models";
import { cn } from "@/lib/utils";

interface TaskSpec {
  id: string;
  name: string;
  /** What the task reads -- the privacy fact behind choosing its provider. */
  reads: string;
  category: string;
  /** The key prefix inside the category ("decide_" for one orders task). */
  prefix: string;
  kind: TaskKind;
  /** Shown beneath the name while the model is empty. */
  offHint?: string;
  advanced: FieldOf[];
}

const TASKS: TaskSpec[] = [
  {
    id: "ai",
    name: "Spam verdicts",
    reads: "Reads every incoming mail to judge whether it is spam.",
    category: "ai",
    prefix: "",
    kind: "chat",
    advanced: [],
  },
  {
    id: "semantic",
    name: "Semantic search",
    reads: "Embeds every mail for search.",
    category: "semantic",
    prefix: "",
    kind: "embedding",
    advanced: [
      { key: "enabled", label: SETTING_LABELS.enabled, control: "switch" },
      { key: "content_chars", label: SETTING_LABELS.content_chars, control: "number", min: 100 },
      { key: "batch_size", label: SETTING_LABELS.batch_size, control: "number", min: 1 },
      { key: "neighbor_k", label: SETTING_LABELS.neighbor_k, control: "number", min: 0 },
      {
        key: "neighbor_min_similarity",
        label: SETTING_LABELS.neighbor_min_similarity,
        control: "number",
        min: 0,
        step: 0.01,
      },
      { key: "max_attempts", label: "Attempts per failed embedding", control: "number", min: 1 },
      { key: "base_delay_seconds", label: "Embedding retry delay (seconds)", control: "number", min: 0 },
      { key: "max_delay_seconds", label: "Embedding retry delay, at most (seconds)", control: "number", min: 0 },
      { key: "query_instructions", label: SETTING_LABELS.query_instructions, control: "json" },
    ],
  },
  {
    id: "assistant",
    name: "Rule assistant",
    reads: "Reads the open mail, plus the subjects and senders of the account's 100 newest mails.",
    category: "assistant",
    prefix: "",
    kind: "chat",
    advanced: [
      { key: "call_timeout_seconds", label: SETTING_LABELS.call_timeout_seconds, control: "number", min: 1 },
    ],
  },
  {
    id: "orders.decide",
    name: "Orders: matching",
    reads: "Reads every mail that passes the first filter, which includes newsletters that mention an order.",
    category: "orders",
    prefix: "decide_",
    kind: "chat",
    advanced: [],
  },
  {
    id: "orders.write",
    name: "Orders: text",
    reads: "Reads only mail already in an order, and its attachments.",
    category: "orders",
    prefix: "write_",
    kind: "chat",
    advanced: [],
  },
  {
    id: "orders.read",
    name: "Orders: image reading",
    reads: "Sees the images of those same mails, and files added by hand, whose OCR read poorly.",
    category: "orders",
    prefix: "read_",
    kind: "chat",
    offHint: "off, OCR only",
    advanced: [
      {
        key: "read_below_confidence",
        label: SETTING_LABELS.read_below_confidence,
        control: "number",
        min: 0,
        step: 1,
      },
    ],
  },
];

const CATEGORIES = Array.from(new Set(TASKS.map((t) => t.category)));

function stringValue(draft: SettingsDraft, category: string, key: string): string {
  const value = draft.value(category, key);
  return typeof value === "string" ? value : "";
}

function titleCase(level: string): string {
  return level.charAt(0).toUpperCase() + level.slice(1);
}

function Labelled({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <span className="text-xs text-muted-foreground @[54rem]:hidden">{label}</span>
      {children}
    </div>
  );
}

// The table is a grid only where the pane is wide enough for six columns;
// narrower, each task is a card with its controls in a small grid beneath
// the name. Container queries, because the pane's width is not the screen's.
const ROW_GRID =
  "@[54rem]:grid @[54rem]:grid-cols-[minmax(11rem,1.3fr)_8.5rem_minmax(10rem,1.5fr)_6.5rem_6.5rem_6.5rem] @[54rem]:items-start @[54rem]:gap-3";
const CONTROLS = "grid grid-cols-2 gap-3 @[30rem]:grid-cols-4 @[54rem]:contents";

function TaskRow({
  spec,
  draft,
  modelsInUse,
}: {
  spec: TaskSpec;
  draft: SettingsDraft;
  modelsInUse: string[];
}) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const { category, prefix, kind } = spec;
  const key = (name: string) => `${prefix}${name}`;
  const provider = stringValue(draft, category, key("provider"));
  const model = stringValue(draft, category, key("model"));
  const effort = draft.value(category, key("reasoning_effort"));
  const maxTokens = draft.value(category, key("max_tokens"));
  const levels = effortLevels(provider);
  const isEmbedding = kind === "embedding";
  const changedKeys = [
    "provider", "model", "base_url", "reasoning_effort", "max_tokens",
    ...spec.advanced.map((f) => f.key.replace(prefix, "")),
  ];
  // A custom provider is unusable without its server address, so the fold
  // that holds it opens by itself until one is entered.
  const needsAddress = provider === "custom" && !draft.value(category, key("base_url"));
  const changed = changedKeys.some((k) => draft.isChanged(category, key(k)));
  const error = changed ? draft.errors[category] : "";

  const showAdvanced = open || needsAddress;

  const advanced: FieldSpec[] = [
    ...(provider === "custom"
      ? [
          {
            category,
            key: key("base_url"),
            label: "Server address",
            help: "An OpenAI-compatible server, for example https://host/v1.",
            control: "text",
            nullable: true,
            placeholder: "https://",
          } as FieldSpec,
        ]
      : []),
    ...spec.advanced.map((f) => ({ ...f, category }) as FieldSpec),
  ];

  const changeProvider = (next: string) => {
    draft.set(category, key("provider"), next);
    if (!isEmbedding) {
      draft.set(category, key("reasoning_effort"), effortAfterProviderChange(effort, next));
    }
  };

  return (
    <div
      data-testid="model-row"
      data-task={spec.id}
      className={cn("flex flex-col gap-3 border-b px-4 py-3 last:border-b-0", changed && "bg-accent/30")}
    >
      <div className={cn("flex flex-col gap-3", ROW_GRID)}>
        <div className="min-w-0">
          <div className="text-sm font-medium">{spec.name}</div>
          <div className="text-xs text-muted-foreground">{spec.reads}</div>
          {spec.offHint && model === "" && (
            <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
              <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-muted-foreground/50" />
              {spec.offHint}
            </div>
          )}
        </div>

        <div className={CONTROLS}>
        <Labelled label="Provider">
          <OptionSelect
            aria-label={`${spec.name}: provider`}
            value={provider}
            options={providerOptions(kind).map((p) => ({ value: p.value, label: p.label }))}
            onChange={(v) => changeProvider(String(v))}
            className="w-full"
          />
        </Labelled>

        <Labelled label="Model">
          <Input
            aria-label={`${spec.name}: model`}
            list={listId}
            value={model}
            placeholder={spec.offHint ? "Choose a model" : "Model name"}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => draft.set(category, key("model"), e.target.value)}
          />
          <datalist id={listId}>
            {modelSuggestions(provider, kind, modelsInUse).map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </Labelled>

        <Labelled label="Effort">
          {isEmbedding ? (
            <span className="px-1 py-1 text-sm text-muted-foreground">—</span>
          ) : (
            <OptionSelect
              aria-label={`${spec.name}: reasoning effort`}
              value={levels.length === 0 ? "" : (effort as string | null | undefined)}
              options={levels.map((l) => ({ value: l, label: titleCase(l) }))}
              placeholder="—"
              disabled={levels.length === 0}
              onChange={(v) => draft.set(category, key("reasoning_effort"), v)}
              className="w-full"
            />
          )}
        </Labelled>

        <Labelled label="Max tokens">
          {isEmbedding ? (
            <span className="px-1 py-1 text-sm text-muted-foreground">—</span>
          ) : (
            <NumberInput
              aria-label={`${spec.name}: max tokens`}
              value={typeof maxTokens === "number" ? maxTokens : 0}
              min={1}
              step={100}
              className="w-full"
              onChange={(n) => draft.set(category, key("max_tokens"), n)}
            />
          )}
        </Labelled>

        </div>

        <div className="flex @[54rem]:justify-end">
          {advanced.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              aria-expanded={showAdvanced}
              disabled={needsAddress}
              onClick={() => setOpen((v) => !v)}
            >
              {showAdvanced ? <ChevronDown className="mr-1 h-3.5 w-3.5" /> : <ChevronRight className="mr-1 h-3.5 w-3.5" />}
              Advanced
            </Button>
          )}
        </div>
      </div>

      {showAdvanced && advanced.length > 0 && (
        <div className="flex flex-col gap-4 rounded-lg border bg-background p-3">
          <SettingFields draft={draft} fields={advanced} />
        </div>
      )}
      {spec.id === "semantic" && <EmbeddingMigrationStatus draft={draft} />}
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

/** Present only while a re-embed runs: the target model differs from the one
 * search still answers from (embeddings/provider.py's active model). */
function EmbeddingMigrationStatus({ draft }: { draft: SettingsDraft }) {
  const active = draft.value("semantic", "active_model");
  const target = draft.value("semantic", "model");
  if (!active || active === target) return null;
  return (
    <div className="rounded-md border bg-muted/30 p-3 text-xs">
      Re-embedding in progress: search still answers from{" "}
      <span className="font-mono">{String(active)}</span> while every message is embedded with{" "}
      <span className="font-mono">{String(target)}</span>. Switches over automatically once coverage
      is complete.
    </div>
  );
}

export function ModelsSettings() {
  const draft = useSettingsDraft();

  if (!draft.ready) {
    return (
      <>
        <CategoryHeader title="Models" />
        <Skeleton className="h-64 w-full" />
      </>
    );
  }

  const modelsInUse = TASKS.map((t) => stringValue(draft, t.category, `${t.prefix}model`));
  return (
    <>
      <CategoryHeader
        title="Models"
        description="One row per task. The key each provider needs is stored under Providers & keys."
      />
      <div className="@container overflow-hidden rounded-xl bg-card ring-1 ring-foreground/10" role="table" aria-label="Models">
        <div
          role="row"
          className={cn("hidden border-b bg-muted/40 px-4 py-2 text-xs font-medium text-muted-foreground @[54rem]:grid", ROW_GRID)}
        >
          <span role="columnheader">Task</span>
          <span role="columnheader">Provider</span>
          <span role="columnheader">Model</span>
          <span role="columnheader">Effort</span>
          <span role="columnheader">Max tokens</span>
          <span />
        </div>
        {TASKS.map((spec) => (
          <TaskRow key={spec.id} spec={spec} draft={draft} modelsInUse={modelsInUse} />
        ))}
      </div>
      <SaveBar
        dirtyCount={draft.count(CATEGORIES)}
        saving={draft.saving}
        onSave={() => draft.save(CATEGORIES)}
        onDiscard={() => draft.discard(CATEGORIES)}
      />
    </>
  );
}
