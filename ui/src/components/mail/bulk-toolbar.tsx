"use client";

import {
  Archive,
  Ban,
  CheckSquare,
  ChevronDown,
  Star,
  Trash2,
  X,
} from "lucide-react";
import { useAtomValue } from "jotai";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useBulkAction, useClearSelection, useSelectAll } from "@/hooks/use-selection";
import { selectionCountAtom, selectionModeAtom, selectionScopeAtom } from "@/store/selection-atom";
import { selectedAccountIdAtom } from "@/lib/atoms";
import { useFolderOrder } from "@/hooks/use-folder-order";

interface BulkToolbarProps {
  folderId: string | null;
  visibleIds: string[];
}

/**
 * Toolbar that appears above the mail list when mails are selected, either
 * as an explicit id list or as a folder-wide scope from "Select all".
 * Provides bulk action buttons for move, archive, star, spam, trash.
 */
export function BulkToolbar({ folderId, visibleIds }: BulkToolbarProps) {
  const count = useAtomValue(selectionCountAtom);
  const scope = useAtomValue(selectionScopeAtom);
  const selectionMode = useAtomValue(selectionModeAtom);
  const accountId = useAtomValue(selectedAccountIdAtom);
  const bulkAction = useBulkAction();
  const clearSelection = useClearSelection();
  const { selectFetched, selectFolderScope } = useSelectAll();
  const { data: orderData } = useFolderOrder(accountId);

  if (!selectionMode || !accountId) return null;

  const folders = orderData?.folders ?? [];
  const scopeLabel =
    scope?.filter === "unread" ? "Every unread message" : "Every message";

  return (
    <div
      role="toolbar"
      aria-label="Bulk actions"
      className="flex items-center gap-2 border-b bg-muted/50 px-3 py-2"
    >
      <Badge variant="secondary" className="mr-1">
        {scope ? scopeLabel : `${count} selected`}
      </Badge>

      {folderId && (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="ghost" size="sm" className="h-7 gap-1 px-2" />}
          >
            <CheckSquare className="h-3.5 w-3.5" />
            Select
            <ChevronDown className="h-3 w-3" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuItem onClick={() => selectFetched(visibleIds)}>
              All loaded ({visibleIds.length})
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => selectFolderScope(folderId, "all")}>
              Every message in this folder
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => selectFolderScope(folderId, "unread")}>
              Every unread message in this folder
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      {/* Move to folder */}
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button variant="ghost" size="sm" className="h-7 gap-1 px-2" />
          }
        >
          Move to
          <ChevronDown className="h-3 w-3" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-60 overflow-y-auto">
          {folders.map((folder) => (
            <DropdownMenuItem
              key={folder.folder_id}
              onClick={() =>
                bulkAction.mutate({
                  accountId,
                  action: "move",
                  targetFolderId: folder.folder_id,
                })
              }
            >
              {folder.imap_name}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      <Button
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2"
        onClick={() => bulkAction.mutate({ accountId, action: "archive" })}
      >
        <Archive className="h-3.5 w-3.5" />
        Archive
      </Button>

      <Button
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2"
        onClick={() => bulkAction.mutate({ accountId, action: "flag" })}
      >
        <Star className="h-3.5 w-3.5" />
        Star
      </Button>

      <Button
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2"
        onClick={() => bulkAction.mutate({ accountId, action: "spam" })}
      >
        <Ban className="h-3.5 w-3.5" />
        Spam
      </Button>

      <Button
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2"
        onClick={() => bulkAction.mutate({ accountId, action: "trash" })}
      >
        <Trash2 className="h-3.5 w-3.5" />
        Trash
      </Button>

      <div className="ml-auto">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2"
          onClick={() => clearSelection()}
          title="Clear selection"
          aria-label="Clear selection"
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}
