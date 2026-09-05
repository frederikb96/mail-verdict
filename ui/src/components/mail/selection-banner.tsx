"use client";

import { ChevronDown, X } from "lucide-react";
import { useAtomValue } from "jotai";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useClearSelection, useSelectAll, useSelection } from "@/hooks/use-selection";
import { useFolders } from "@/hooks/use-folders";
import { selectionModeAtom } from "@/store/selection-atom";

interface SelectionBannerProps {
  /** null in the unified view -- a predicate is only ever minted over one
   * account's folder, so this offer doesn't apply there. */
  accountId: string | null;
  folderId: string | null;
  /** A threaded row is a conversation, not a message -- the predicate
   * counts messages, so offering it here would contradict the rows shown. */
  threaded: boolean;
  loadedCount: number;
}

/**
 * A thin bar above the list, shown for any active selection (one row or
 * many): the count, a way to extend to the whole folder, and a clear
 * button. Bulk actions themselves live in the reading pane's own panel
 * once more than one row is selected -- this bar only ever offers
 * selection itself, the way Outlook's does.
 */
export function SelectionBanner({
  accountId, folderId, threaded, loadedCount,
}: SelectionBannerProps) {
  const selectionMode = useAtomValue(selectionModeAtom);
  const { count, state } = useSelection();
  const clearSelection = useClearSelection();
  const { selectFolderScope } = useSelectAll();
  const { data: folders } = useFolders(accountId);
  const folder = folders?.find((f) => f.id === folderId);

  if (!selectionMode) return null;

  const canOfferFolder = !threaded && !!accountId && !!folderId && !state.predicate;
  // The discoverable path: once every currently loaded row is ticked by
  // hand, offer to extend to the rest of the folder rather than making the
  // user find a menu for it.
  const everyLoadedRowTicked = canOfferFolder && loadedCount > 0 && count >= loadedCount;
  const folderTotal = folder?.total_count ?? 0;

  return (
    <div
      role="toolbar"
      aria-label="Selection"
      className="flex items-center gap-2 border-b bg-muted/50 px-3 py-2 text-sm"
    >
      <Badge variant="secondary">{count} selected</Badge>

      {state.predicate ? (
        <span className="text-xs text-muted-foreground">
          {state.predicate.filter === "unread" ? "Every unread message" : "Every message"} in this folder
        </span>
      ) : everyLoadedRowTicked && folderTotal > loadedCount ? (
        <Button
          variant="link"
          size="sm"
          className="h-auto p-0 text-xs"
          onClick={() => selectFolderScope(accountId!, folderId!, "all")}
        >
          Select all {folderTotal} messages in {folder?.display_name ?? folder?.imap_name}
        </Button>
      ) : null}

      {canOfferFolder && (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="ghost" size="sm" className="h-7 gap-1 px-2" />}
          >
            Select
            <ChevronDown className="h-3 w-3" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuItem
              onClick={async () => {
                await selectFolderScope(accountId!, folderId!, "all");
              }}
            >
              Every message in this folder
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={async () => {
                await selectFolderScope(accountId!, folderId!, "unread");
              }}
            >
              Every unread message in this folder
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      <div className="ml-auto">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2"
          onClick={clearSelection}
          title="Clear selection"
          aria-label="Clear selection"
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}
