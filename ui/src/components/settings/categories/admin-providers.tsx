"use client";

import { AlertTriangle, ShieldCheck } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { CategoryHeader, SectionCard } from "@/components/settings/blocks";
import { ProviderKeyRow, type KeyDot } from "@/components/settings/provider-key-row";
import { useProvidersStatus } from "@/hooks/use-settings";
import { format as formatDate } from "date-fns";

const PROVIDERS = [
  {
    key: "anthropic",
    label: "Anthropic",
    clearConsequence:
      "Tasks that use Anthropic fall back to the environment variable, if one is set, or stop until a key is stored again.",
  },
  {
    key: "openai",
    label: "OpenAI",
    clearConsequence:
      "Tasks that use OpenAI fall back to the environment variable, if one is set, or stop until a key is stored again.",
  },
  {
    key: "custom",
    label: "Custom (OpenAI-compatible)",
    help: "The server address is set per task under Models.",
    clearConsequence:
      "Tasks that use a custom server fall back to the environment variable, if one is set, or stop until a key is stored again.",
  },
];

export function ProvidersSettings() {
  const { data: status, isLoading } = useProvidersStatus();
  const unhealthy = (status?.breakers ?? []).filter((b) => b.state !== "closed");
  // A circuit is named `<provider>:<task>`.
  const failingProviders = new Set(unhealthy.map((b) => b.name.split(":")[0]));

  const dotOf = (key: string): KeyDot => {
    if (failingProviders.has(key)) return "failing";
    return status?.providers[key]?.configured ? "ok" : "none";
  };

  return (
    <>
      <CategoryHeader
        title="Providers & keys"
        description="Keys are encrypted at rest and never shown again once saved. One key per provider serves every task that names it."
      />
      <SectionCard icon={ShieldCheck} title="API keys">
        {isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : (
          <div className="divide-y">
            {PROVIDERS.map((p) => (
              <ProviderKeyRow
                key={p.key}
                providerKey={p.key}
                label={p.label}
                status={status?.providers[p.key]}
                dot={dotOf(p.key)}
                help={p.help}
                clearConsequence={p.clearConsequence}
              />
            ))}
          </div>
        )}
      </SectionCard>
      {unhealthy.length > 0 && (
        <SectionCard icon={AlertTriangle} title="Task health" description="Tasks whose provider is refusing or backing off.">
          <ul className="flex flex-col gap-2 text-sm" data-testid="task-health">
            {unhealthy.map((b) => (
              <li key={b.name} className="flex flex-col gap-0.5 rounded-md border border-destructive/40 bg-destructive/5 p-3">
                <span>
                  <span className="font-mono">{b.name}</span>{" "}
                  {b.state === "suspended" ? "is suspended" : "is backing off"}
                  {b.reason ? `: ${b.reason}` : ""}
                </span>
                {b.retry_after && (
                  <span className="text-xs text-muted-foreground">
                    next attempt at {formatDate(new Date(b.retry_after), "HH:mm")}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </SectionCard>
      )}
    </>
  );
}
