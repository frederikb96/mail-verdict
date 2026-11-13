"use client";

/**
 * One provider's API key: a status line, "Replace key" revealing a password
 * field in place, and Clear behind a confirmation.
 *
 * The field always starts empty -- there is nothing to prefill it with, a
 * stored key is never sent back -- so nothing is written until a key is
 * typed and saved; an idle row can never wipe a key nobody meant to touch.
 */

import { useState, type ReactNode } from "react";
import { MoreVertical } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { useUpdateSettings } from "@/hooks/use-settings";
import { cn } from "@/lib/utils";
import type { ProviderKeyStatus } from "@/types/api";

export type KeyDot = "ok" | "none" | "failing";

const DOT_CLASS: Record<KeyDot, string> = {
  ok: "bg-green-500",
  none: "bg-muted-foreground/40",
  failing: "bg-destructive",
};

const DOT_LABEL: Record<KeyDot, string> = {
  ok: "Configured",
  none: "Not configured",
  failing: "Failing",
};

export function ProviderKeyRow({
  providerKey,
  label,
  status,
  dot,
  help,
  clearConsequence,
}: {
  /** The settings field is `${providerKey}_api_key`. */
  providerKey: string;
  label: string;
  status: ProviderKeyStatus | undefined;
  dot: KeyDot;
  help?: ReactNode;
  /** What clearing the key leads to, shown in the confirmation. */
  clearConsequence: string;
}) {
  const [replacing, setReplacing] = useState(false);
  const [value, setValue] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const updateSettings = useUpdateSettings();
  const configured = status?.configured === true;
  const fieldName = `${providerKey}_api_key`;

  const write = (next: string, done: () => void) => {
    updateSettings.mutate({ category: "ai", data: { [fieldName]: next } }, { onSuccess: done });
  };

  const cancel = () => {
    setReplacing(false);
    setValue("");
  };

  return (
    <div data-testid="provider-row" data-provider={providerKey} className="flex flex-col gap-2 py-3">
      <div className="flex items-center gap-3">
        <span
          role="img"
          aria-label={DOT_LABEL[dot]}
          title={DOT_LABEL[dot]}
          className={cn("h-2.5 w-2.5 shrink-0 rounded-full", DOT_CLASS[dot])}
        />
        <div className="flex min-w-0 flex-1 flex-col sm:flex-row sm:items-baseline sm:gap-3">
          <span className="text-sm font-medium">{label}</span>
          <span className="text-xs text-muted-foreground">
            {configured ? `configured (…${status?.hint})` : "not configured"}
          </span>
        </div>
        {!replacing && (
          <Button variant="outline" size="sm" onClick={() => setReplacing(true)}>
            {configured ? "Replace key" : "Add key"}
          </Button>
        )}
        {configured && (
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 shrink-0"
                  aria-label={`Actions for the ${label} key`}
                />
              }
            >
              <MoreVertical className="h-4 w-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem variant="destructive" onClick={() => setConfirmClear(true)}>
                Clear key
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {replacing && (
        <form
          className="flex flex-col gap-2 pl-5 sm:flex-row"
          onSubmit={(e) => {
            e.preventDefault();
            if (value !== "") write(value, cancel);
          }}
        >
          <Input
            autoFocus
            type="password"
            autoComplete="off"
            aria-label={`${label} API key`}
            placeholder="Paste key"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") cancel();
            }}
          />
          <div className="flex gap-2">
            <Button
              type="submit"
              size="sm"
              aria-label={`Store the ${label} key`}
              disabled={value === "" || updateSettings.isPending}
            >
              Save
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={cancel}>
              Cancel
            </Button>
          </div>
        </form>
      )}
      {help && <p className="pl-5 text-xs text-muted-foreground">{help}</p>}
      <ConfirmDialog
        open={confirmClear}
        onOpenChange={setConfirmClear}
        title={`Clear the ${label} key?`}
        description={clearConsequence}
        confirmLabel="Clear key"
        isConfirming={updateSettings.isPending}
        onConfirm={() => write("", () => setConfirmClear(false))}
      />
    </div>
  );
}
