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
}

export function FolderPicker({ selectedIds, onChange }: FolderPickerProps) {
  const { options, isLoading } = useSearchFolders();
  const allIds = useMemo(() => options.map((o) => o.folder.id), [options]);

  const effectiveSelected = selectedIds ?? allIds;
  const selectedSet = useMemo(() => new Set(effectiveSelected), [effectiveSelected]);

  const groups = useMemo(() => {
    const byAccount = new Map<string, { accountName: string; options: typeof options }>();
    for (const opt of options) {
      const entry = byAccount.get(opt.accountId) ?? { accountName: opt.accountName, options: [] };
      entry.options.push(opt);
      byAccount.set(opt.accountId, entry);
    }
    return Array.from(byAccount.values());
  }, [options]);

  const toggleFolder = (id: string, checked: boolean) => {
    const next = new Set(effectiveSelected);
    if (checked) next.add(id);
    else next.delete(id);
    // Selecting back up to everything collapses to "all" (null) rather
    // than writing out every id, so a folder created later is still
    // included by default instead of silently excluded.
    onChange(next.size >= allIds.length ? null : Array.from(next));
  };

  const label =
    selectedIds === null || effectiveSelected.length === allIds.length
      ? "All folders"
      : effectiveSelected.length === 0
        ? "No folders"
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
          <div className="flex gap-3">
            <button type="button" className="text-primary hover:underline" onClick={() => onChange(null)}>
              Select all
            </button>
            <button type="button" className="text-primary hover:underline" onClick={() => onChange([])}>
              Deselect all
            </button>
          </div>
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
