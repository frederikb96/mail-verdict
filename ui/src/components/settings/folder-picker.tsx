"use client";

/**
 * Pick folders from any number of accounts: a filter, a switch that hides
 * system folders, and one collapsible group per account whose checkbox
 * ticks everything the account lists. Used wherever a set of folders is
 * chosen (unified views, notifications).
 */

import { useId, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useFolders } from "@/hooks/use-folders";
import {
  groupState,
  listedFolders,
  toggleFolder,
  toggleGroup,
} from "@/lib/folder-picker";
import { folderDisplayName } from "@/lib/folders";
import { cn } from "@/lib/utils";
import type { AccountResponse } from "@/types/api";

interface FolderPickerProps {
  /** Ids of the ticked folders. */
  value: string[];
  onChange: (ids: string[]) => void;
  /** The accounts to choose from -- the signed-in person's own. */
  accounts: AccountResponse[];
  hideSystemDefault?: boolean;
  className?: string;
}

interface GroupProps {
  account: AccountResponse;
  value: string[];
  onChange: (ids: string[]) => void;
  hideSystem: boolean;
  query: string;
  /** Open regardless of the user's own toggle (one account, or a query). */
  forceOpen: boolean;
}

function AccountGroup({ account, value, onChange, hideSystem, query, forceOpen }: GroupProps) {
  const { data: folders, isLoading } = useFolders(account.id);
  const [open, setOpen] = useState(false);
  const selected = useMemo(() => new Set(value), [value]);
  const listed = useMemo(
    () => listedFolders(folders ?? [], account.name, { hideSystem, query, selected }),
    [folders, account.name, hideSystem, query, selected],
  );
  const listedIds = listed.map((f) => f.id);
  const state = groupState(listedIds, selected);
  const ticked = listedIds.filter((id) => selected.has(id)).length;
  const expanded = forceOpen || open;
  const bodyId = useId();

  // A query that matches nothing in this account removes it from the list.
  if (query.trim() !== "" && !isLoading && listed.length === 0) return null;

  return (
    <div data-testid="folder-picker-account" data-account-id={account.id} className="border-b last:border-b-0">
      <div className="flex items-center gap-2 bg-muted/40 px-2 py-1.5">
        <Checkbox
          checked={state === "all"}
          indeterminate={state === "some"}
          disabled={listedIds.length === 0}
          aria-label={`All folders of ${account.name}`}
          onCheckedChange={() => onChange(toggleGroup(value, listedIds))}
        />
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={bodyId}
          disabled={forceOpen}
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left text-sm font-medium"
        >
          {account.emoji && <span aria-hidden>{account.emoji}</span>}
          <span className="truncate">{account.name}</span>
          <span className="ml-auto shrink-0 text-xs font-normal text-muted-foreground">
            {ticked} of {listedIds.length}
          </span>
          {!forceOpen &&
            (expanded ? (
              <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
            ))}
        </button>
      </div>
      {expanded && (
        <div id={bodyId} className="flex flex-col py-0.5">
          {isLoading && (
            <div className="flex flex-col gap-1 px-3 py-1">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-5 w-full" />
              ))}
            </div>
          )}
          {!isLoading && (folders ?? []).length === 0 && (
            <p className="px-3 py-2 text-sm text-muted-foreground">No folders synced yet</p>
          )}
          {listed.map((folder) => (
            <label
              key={folder.id}
              data-testid="folder-picker-folder"
              data-folder-id={folder.id}
              className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm hover:bg-accent/50"
            >
              <Checkbox
                data-picker-item
                checked={selected.has(folder.id)}
                onCheckedChange={(checked) => onChange(toggleFolder(value, folder.id, checked === true))}
              />
              <span className="truncate">{folderDisplayName(folder)}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

export function FolderPicker({
  value,
  onChange,
  accounts,
  hideSystemDefault = true,
  className,
}: FolderPickerProps) {
  const [query, setQuery] = useState("");
  const [hideSystem, setHideSystem] = useState(hideSystemDefault);
  const searching = query.trim() !== "";

  // Arrow keys move between the boxes; Space ticks the focused one.
  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const items = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>("[data-picker-item]"),
    );
    const at = items.indexOf(document.activeElement as HTMLElement);
    if (at === -1) return;
    const next = items[at + (event.key === "ArrowDown" ? 1 : -1)];
    if (!next) return;
    event.preventDefault();
    next.focus();
  };

  return (
    <div className={cn("flex flex-col gap-2", className)} data-testid="folder-picker">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-40 flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter folders"
            aria-label="Filter folders"
            className="pl-8"
          />
        </div>
        <Button
          variant={hideSystem ? "default" : "outline"}
          size="sm"
          aria-pressed={hideSystem}
          onClick={() => setHideSystem((v) => !v)}
        >
          Hide system folders
        </Button>
      </div>
      <div className="rounded-lg border" onKeyDown={onKeyDown}>
        {accounts.length === 0 && (
          <p className="px-3 py-2 text-sm text-muted-foreground">No accounts yet</p>
        )}
        {accounts.map((account) => (
          <AccountGroup
            key={account.id}
            account={account}
            value={value}
            onChange={onChange}
            hideSystem={hideSystem}
            query={query}
            forceOpen={accounts.length === 1 || searching}
          />
        ))}
      </div>
    </div>
  );
}
