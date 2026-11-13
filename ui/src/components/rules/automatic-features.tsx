"use client";

/**
 * The two features that work on mail as it arrives, per account: spam
 * detection and orders & tickets. They are the user-facing face of the
 * classify and orders steps in the rule list.
 */

import { useState } from "react";
import { Zap } from "lucide-react";

import { OrderCatchUpDialog } from "@/components/accounts/order-catch-up-dialog";
import { SectionCard } from "@/components/settings/blocks";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useAccounts, useUpdateAccount } from "@/hooks/use-accounts";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api";
import type { AccountResponse } from "@/types/api";

function FeatureSwitch({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <Switch checked={checked} disabled={disabled} onCheckedChange={onChange} aria-label={label} />
      <span>{label}</span>
    </label>
  );
}

function AccountFeatureRow({ account }: { account: AccountResponse }) {
  const update = useUpdateAccount();
  const { push } = useToast();
  const [catchUpOpen, setCatchUpOpen] = useState(false);

  const set = (data: { spam_enabled?: boolean; orders_enabled?: boolean }) =>
    update.mutate(
      { id: account.id, data },
      {
        onError: (err) =>
          push(err instanceof ApiError ? err.message : "Could not save the change", "error"),
      },
    );

  return (
    <div
      data-testid="feature-row"
      className="flex flex-col gap-2 border-b py-3 first:pt-0 last:border-b-0 last:pb-0 sm:flex-row sm:items-center sm:gap-6"
    >
      <div className="min-w-0 break-words text-sm font-medium sm:w-44 sm:shrink-0">
        {account.emoji ? `${account.emoji} ` : ""}
        {account.name}
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <FeatureSwitch
          label="Spam detection"
          checked={account.spam_enabled}
          disabled={update.isPending}
          onChange={(spam_enabled) => set({ spam_enabled })}
        />
        <FeatureSwitch
          label="Orders & tickets"
          checked={account.orders_enabled}
          disabled={update.isPending}
          onChange={(orders_enabled) => set({ orders_enabled })}
        />
        {account.orders_enabled && (
          <Button variant="link" size="sm" className="h-auto p-0" onClick={() => setCatchUpOpen(true)}>
            Look through recent mail…
          </Button>
        )}
      </div>
      {account.orders_enabled && (
        <OrderCatchUpDialog accountId={account.id} open={catchUpOpen} onOpenChange={setCatchUpOpen} />
      )}
    </div>
  );
}

export function AutomaticFeatures() {
  const { data: accounts, isLoading } = useAccounts();
  return (
    <SectionCard
      icon={Zap}
      title="Automatic features"
      description="Per account. Spam the model flags is moved to Junk and listed under Spam review; which model reads your mail is set by the administrator."
    >
      {isLoading && <Skeleton className="h-16 w-full" />}
      {accounts?.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No accounts yet. Add one under Settings, then switch these on here.
        </p>
      )}
      <div>
        {accounts?.map((account) => (
          <AccountFeatureRow key={account.id} account={account} />
        ))}
      </div>
    </SectionCard>
  );
}
