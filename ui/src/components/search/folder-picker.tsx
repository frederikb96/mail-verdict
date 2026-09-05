"use client";

/**
 * Multi-select folder scope for the search page. A Popover with plain
 * checkboxes rather than the Combobox primitive used elsewhere (e.g.
 * recipient-field.tsx): a folder list is browsed, not typed into, and a
 * checkbox list sidesteps the combobox's own aria-hide-the-rest-of-the-
 * page-while-open behaviour that a typeahead popup needs and this
 * doesn't.
 */

import { useMemo } from "react";
import { ChevronDown, Folder as FolderIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useSearchFolders } from "@/hooks/use-search-folders";

interface FolderPickerProps {
  /** null means every folder -- see search-prefs.ts. */
  selectedIds: string[] | null;
  onChange: (ids: string[] | null) => void;
  /** Narrows the offered (and counted) folders to one account, matching
   * whatever account the search itself is scoped to -- omitted (or the
   * unified view) offers every account's folders. Without this, picking
   * a folder that belongs to an account the search isn't scoped to
   * produces a query that can never match, presented as "No results". */
  accountId?: string;
}

export function FolderPicker({ selectedIds, onChange, accountId }: FolderPickerProps) {
  const { options, isLoading } = useSearchFolders(accountId);
  const allIds = useMemo(() => options.map((o) => o.folder.id), [options]);

  // An explicit [] is a real, distinct state -- "nothing selected",
  // which search-page.tsx reads as "search disabled, choose a folder"
  // rather than silently sending an absent restriction (see setSelection
  // and search-page.tsx's hasFolderScope). Only a stored `null` means
  // "every folder".
  const effectiveSelected = selectedIds === null ? allIds : selectedIds;
  const selectedSet = useMemo(() => new Set(effectiveSelected), [effectiveSelected]);
  const allSelected = allIds.length > 0 && effectiveSelected.length === allIds.length;

  const groups = useMemo(() => {
    const byAccount = new Map<string, { accountName: string; options: typeof options }>();
    for (const opt of options) {
      const entry = byAccount.get(opt.accountId) ?? { accountName: opt.accountName, options: [] };
      entry.options.push(opt);
      byAccount.set(opt.accountId, entry);
    }
    return Array.from(byAccount.values());
  }, [options]);

  const setSelection = (nextIds: string[]) => {
    // An empty selection is now a real, meaningful state -- see
    // effectiveSelected's comment above -- so it is passed through
    // rather than refused. Selecting back up to everything collapses to
    // "all" (null) rather than writing out every id, so a folder created
    // later is still included by default instead of silently excluded.
    onChange(nextIds.length >= allIds.length ? null : nextIds);
  };

  const toggleFolder = (id: string, checked: boolean) => {
    const next = new Set(effectiveSelected);
    if (checked) next.add(id);
    else next.delete(id);
    setSelection(Array.from(next));
  };

  const label =
    effectiveSelected.length === 0
      ? "No folders"
      : allSelected
        ? "All folders"
        : `${effectiveSelected.length} of ${allIds.length} folders`;

  return (
    <Popover>
      <PopoverTrigger render={<Button variant="outline" size="sm" className="gap-1.5" />}>
        <FolderIcon className="h-3.5 w-3.5" />
        {label}
        <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 p-0">
        <div className="flex items-center justify-between border-b px-3 py-2 text-xs">
          <span className="font-medium text-muted-foreground">Search in</span>
          {/* Toggles between the two states a real empty selection makes
              possible: with everything ticked, clearing it is now
              reachable; from anywhere short of that, the button restores
              "all" in one step rather than requiring every box checked
              by hand. */}
          <button
            type="button"
            className="text-primary hover:underline"
            onClick={() => onChange(allSelected ? [] : null)}
          >
            {allSelected ? "Deselect all" : "Select all"}
          </button>
        </div>
        <div className="max-h-72 overflow-y-auto p-1">
          {isLoading && (
            <div className="px-2 py-3 text-xs text-muted-foreground">Loading folders…</div>
          )}
          {!isLoading && options.length === 0 && (
            <div className="px-2 py-3 text-xs text-muted-foreground">No folders</div>
          )}
          {groups.map((group) => (
            <div key={group.accountName}>
              {groups.length > 1 && (
                <div className="px-2 pt-2 pb-1 text-xs font-medium text-muted-foreground">
                  {group.accountName}
                </div>
              )}
              {group.options.map(({ folder }) => (
                <label
                  key={folder.id}
                  className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent/50"
                >
                  <Checkbox
                    checked={selectedSet.has(folder.id)}
                    onCheckedChange={(checked) => toggleFolder(folder.id, checked === true)}
                  />
                  <span className="truncate">{folder.display_name ?? folder.imap_name}</span>
                </label>
              ))}
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}
