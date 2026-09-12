"use client";

/**
 * "Move to..." for a single message -- the reading pane toolbar's button
 * and the `v` shortcut both open this. A type-to-filter list over the
 * message's own account (never the unified view's cross-account list
 * bulk-panel.tsx offers: one message always belongs to exactly one
 * account, so there is nothing to resolve per-account here), with the
 * folders most recently moved to in this account listed first.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { FolderInput } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useFolderOrder } from "@/hooks/use-folder-order";
import { folderDisplayName } from "@/lib/folders";
import type { FolderOrderItem } from "@/types/api";

const RECENT_FOLDERS_KEY = "mailverdict:recent-move-folders";
const MAX_RECENT = 5;

function readRecent(accountId: string): string[] {
  if (typeof window === "undefined") return [];
  try {
    const all = JSON.parse(window.localStorage.getItem(RECENT_FOLDERS_KEY) ?? "{}");
    return Array.isArray(all[accountId]) ? all[accountId] : [];
  } catch {
    return [];
  }
}

function pushRecent(accountId: string, folderId: string): void {
  if (typeof window === "undefined") return;
  try {
    const all = JSON.parse(window.localStorage.getItem(RECENT_FOLDERS_KEY) ?? "{}");
    const current: string[] = Array.isArray(all[accountId]) ? all[accountId] : [];
    const next = [folderId, ...current.filter((id) => id !== folderId)].slice(0, MAX_RECENT);
    window.localStorage.setItem(
      RECENT_FOLDERS_KEY,
      JSON.stringify({ ...all, [accountId]: next }),
    );
  } catch {
    // Recents are a convenience; losing them costs nothing else.
  }
}

interface MoveToFolderPopoverProps {
  accountId: string;
  /** Excluded from the list -- moving a message into the folder it is
   * already in is a no-op the picker does not need to offer. */
  currentFolderId: string | null;
  onMove: (folderId: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  trigger: React.ReactElement;
}

export function MoveToFolderPopover({
  accountId,
  currentFolderId,
  onMove,
  open,
  onOpenChange,
  trigger,
}: MoveToFolderPopoverProps) {
  const { data: orderData } = useFolderOrder(accountId);
  const [filter, setFilter] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const [activeIndex, setActiveIndex] = useState(0);

  const folders = useMemo(
    () => (orderData?.folders ?? []).filter((f) => f.folder_id !== currentFolderId),
    [orderData, currentFolderId],
  );

  const ordered = useMemo(() => {
    if (filter.trim()) {
      const needle = filter.trim().toLowerCase();
      return folders.filter((f) => folderDisplayName(f).toLowerCase().includes(needle));
    }
    const recentIds = readRecent(accountId);
    const byId = new Map(folders.map((f) => [f.folder_id, f]));
    const recent = recentIds.map((id) => byId.get(id)).filter((f): f is FolderOrderItem => !!f);
    const rest = folders.filter((f) => !recentIds.includes(f.folder_id));
    return [...recent, ...rest];
  }, [folders, filter, accountId]);

  useEffect(() => {
    if (open) {
      setFilter("");
      setActiveIndex(0);
      // The popover's own open animation mounts the input a beat before
      // it can accept focus -- matches the pattern the recipient
      // combobox and other autofocus-on-open popovers already use.
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [filter]);

  const choose = (folderId: string) => {
    pushRecent(accountId, folderId);
    onMove(folderId);
    onOpenChange(false);
  };

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger render={trigger} />
      <PopoverContent align="end" className="w-64 p-0">
        <div className="border-b p-2">
          <Input
            ref={inputRef}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Move to…"
            aria-label="Filter folders to move to"
            className="h-7 text-xs"
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setActiveIndex((i) => Math.min(i + 1, ordered.length - 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setActiveIndex((i) => Math.max(i - 1, 0));
              } else if (e.key === "Enter") {
                e.preventDefault();
                const target = ordered[activeIndex];
                if (target) choose(target.folder_id);
              } else if (e.key === "Escape") {
                onOpenChange(false);
              }
            }}
          />
        </div>
        <div role="listbox" aria-label="Folders" className="max-h-64 overflow-y-auto p-1">
          {ordered.length === 0 && (
            <div className="px-2 py-3 text-xs text-muted-foreground">No matching folders</div>
          )}
          {ordered.map((folder, i) => (
            <button
              key={folder.folder_id}
              type="button"
              role="option"
              aria-selected={i === activeIndex}
              className={
                "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm " +
                (i === activeIndex ? "bg-accent" : "hover:bg-accent/50")
              }
              onMouseEnter={() => setActiveIndex(i)}
              onClick={() => choose(folder.folder_id)}
            >
              <FolderInput className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="truncate">{folderDisplayName(folder)}</span>
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}
