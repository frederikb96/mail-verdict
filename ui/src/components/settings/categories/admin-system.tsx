"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Cpu, Database, Loader2, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CategoryHeader, SaveBar, SectionCard } from "@/components/settings/blocks";
import { ProviderKeyRow } from "@/components/settings/provider-key-row";
import { CategoryFields } from "@/components/settings/setting-fields";
import { useEmbeddingBackfill, useEmbeddingStatus } from "@/hooks/use-embedding-status";
import { useProvidersStatus } from "@/hooks/use-settings";
import { useSettingsDraft } from "@/hooks/use-settings-draft";
import { formatRelativeAgo } from "@/lib/format";
import type { UsageExportStatus } from "@/types/api";

/** Worker mechanics of the queues: whole categories, plus the orders keys
 * that belong to its queue rather than to its model tasks. */
const TUNING_CATEGORIES = ["pipeline", "retry", "orders"];
const ORDERS_QUEUE_KEYS = [
  "lease_seconds",
  "poll_interval_seconds",
  "max_attempts",
  "base_delay_seconds",
  "max_delay_seconds",
  "call_timeout_seconds",
];

/** How the token usage push to Elasticsearch is going: nothing while it has
 * no key, the last request's error while it fails, else the last delivery. */
function UsageExportLine({ status }: { status: UsageExportStatus | undefined }) {
  if (!status?.key.configured) {
    return (
      <p className="text-xs text-muted-foreground">
        The token usage of every model call is recorded; it is pushed to Elasticsearch once an API
        key is stored here.
      </p>
    );
  }
  if (status.last_error) {
    return (
      <p className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-xs">
        Usage export failing ({formatRelativeAgo(status.last_error_at)}, {status.pending} waiting):{" "}
        {status.last_error}
      </p>
    );
  }
  return (
    <p className="text-xs text-muted-foreground">
      Usage export:{" "}
      {status.last_success_at
        ? `last delivered ${formatRelativeAgo(status.last_success_at)}`
        : "nothing delivered yet"}
      , {status.pending} waiting.
    </p>
  );
}

function SemanticIndexCard() {
  const { data: status, isLoading } = useEmbeddingStatus();
  const backfill = useEmbeddingBackfill();
  return (
    <SectionCard
      icon={Database}
      title="Semantic search index"
      description="How many mails have an embedding from the configured model. Coverage seldom reaches 100%: some mails never embed."
    >
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {status && (
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="text-sm" data-testid="embedding-status">
            <span className="font-mono">{status.model}</span>
            {" — "}
            {Math.round(status.coverage * 100)}% of {status.in_scope} mails searchable
            <span className="text-muted-foreground">
              {" "}
              ({status.pending} pending, {status.failed} failed)
              {!status.active && ", not serving search yet"}
            </span>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="shrink-0 self-start"
            disabled={backfill.isPending}
            onClick={() => backfill.mutate()}
          >
            {backfill.isPending && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            Fill missing now
          </Button>
        </div>
      )}
    </SectionCard>
  );
}

export function SystemSettings() {
  const draft = useSettingsDraft();
  const { data: providers } = useProvidersStatus();
  const [tuningOpen, setTuningOpen] = useState(false);

  return (
    <>
      <CategoryHeader title="System" />

      <SectionCard icon={Upload} title="Usage export">
        <ProviderKeyRow
          providerKey="elastic_usage"
          label="Elastic usage export"
          status={providers?.usage_export.key}
          dot={providers?.usage_export.last_error ? "failing" : providers?.usage_export.key.configured ? "ok" : "none"}
          clearConsequence="Usage stops being exported; it is still recorded and is pushed again once a key is stored."
        />
        <UsageExportLine status={providers?.usage_export} />
      </SectionCard>

      <SemanticIndexCard />

      <SectionCard icon={Cpu} title="Processing tuning" description="How the queues claim, retry and time out their work. The defaults suit most mailboxes.">
        <Button
          variant="outline"
          size="sm"
          className="self-start"
          aria-expanded={tuningOpen}
          onClick={() => setTuningOpen((v) => !v)}
        >
          {tuningOpen ? <ChevronDown className="mr-1 h-3.5 w-3.5" /> : <ChevronRight className="mr-1 h-3.5 w-3.5" />}
          {tuningOpen ? "Hide settings" : "Show settings"}
        </Button>
        {tuningOpen && draft.ready && (
          <div className="flex flex-col gap-6">
            <section className="flex flex-col gap-4">
              <h3 className="text-sm font-semibold">Pipeline</h3>
              <CategoryFields draft={draft} category="pipeline" />
            </section>
            <section className="flex flex-col gap-4">
              <h3 className="text-sm font-semibold">Model call retries</h3>
              <CategoryFields draft={draft} category="retry" />
            </section>
            <section className="flex flex-col gap-4">
              <h3 className="text-sm font-semibold">Orders queue</h3>
              <CategoryFields draft={draft} category="orders" keys={ORDERS_QUEUE_KEYS} />
            </section>
          </div>
        )}
      </SectionCard>

      {Object.entries(draft.errors).map(
        ([category, message]) =>
          message && (
            <p key={category} role="alert" className="text-sm text-destructive">
              Could not save {category} settings: {message}
            </p>
          ),
      )}
      <SaveBar
        dirtyCount={draft.count(TUNING_CATEGORIES)}
        saving={draft.saving}
        onSave={() => draft.save(TUNING_CATEGORIES)}
        onDiscard={() => draft.discard(TUNING_CATEGORIES)}
      />
    </>
  );
}
