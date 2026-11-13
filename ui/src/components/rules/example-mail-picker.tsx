"use client";

/**
 * Pick one of your recent mails as the example a rule is described or
 * tested against. Lists the newest mail of an account's Inbox, with a box
 * that narrows the list by sender or subject.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { OptionSelect } from "@/components/settings/setting-fields";
import { useAccounts } from "@/hooks/use-accounts";
import { useFolders } from "@/hooks/use-folders";
import { api } from "@/lib/api";
import { extractSenderName, formatRelativeDate } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { MessageSummary } from "@/types/api";

const RECENT = 25;

export function ExampleMailPicker({
  selectedId,
  onPick,
}: {
  selectedId: string | null;
  onPick: (mail: MessageSummary) => void;
}) {
  const { data: accounts } = useAccounts();
  const [accountId, setAccountId] = useState<string>("");
  const [filter, setFilter] = useState("");
  const active = (accounts ?? []).filter((a) => a.is_active);
  const account = active.find((a) => a.id === accountId) ?? active[0];
  const { data: folders } = useFolders(account?.id ?? null);
  const inbox = folders?.find((f) => f.special_use === "inbox");

  const { data, isLoading } = useQuery({
    queryKey: ["rule-example-mails", account?.id, inbox?.id],
    queryFn: () => api.mails.list({ account_id: account!.id, folder_id: inbox!.id, limit: RECENT }),
    enabled: !!account && !!inbox,
    staleTime: 30_000,
  });

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const mails = data?.messages ?? [];
    if (!needle) return mails;
    return mails.filter((m) =>
      `${m.from_addr ?? ""} ${m.subject ?? ""}`.toLowerCase().includes(needle),
    );
  }, [data, filter]);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        {active.length > 1 && (
          <OptionSelect
            aria-label="Account"
            value={account?.id}
            options={active.map((a) => ({ value: a.id, label: a.name }))}
            className="w-44"
            onChange={(v) => setAccountId(String(v))}
          />
        )}
        <Input
          aria-label="Filter recent mail"
          placeholder="Filter by sender or subject"
          value={filter}
          className="min-w-40 flex-1"
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      <div
        role="listbox"
        aria-label="Recent mail"
        className="max-h-72 overflow-y-auto rounded-lg border"
      >
        {(isLoading || !inbox) && (
          <div className="flex flex-col gap-2 p-3">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
          </div>
        )}
        {!isLoading && inbox && shown.length === 0 && (
          <p className="p-3 text-sm text-muted-foreground">No mail to show.</p>
        )}
        {shown.map((mail) => (
          <button
            key={mail.id}
            type="button"
            role="option"
            aria-selected={mail.id === selectedId}
            onClick={() => onPick(mail)}
            className={cn(
              "flex w-full flex-col gap-0.5 border-b px-3 py-2 text-left last:border-b-0 hover:bg-muted/60",
              mail.id === selectedId && "bg-accent",
            )}
          >
            <span className="flex items-baseline justify-between gap-2">
              <span className="min-w-0 break-words text-sm font-medium">
                {extractSenderName(mail.from_addr)}
              </span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {formatRelativeDate(mail.received_at)}
              </span>
            </span>
            <span className="break-words text-xs text-muted-foreground">
              {mail.subject || "(no subject)"}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
