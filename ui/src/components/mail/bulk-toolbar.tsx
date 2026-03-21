"use client";

import {
  Archive,
  Ban,
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
import { useBulkAction, useClearSelection } from "@/hooks/use-selection";
import { selectionCountAtom } from "@/store/selection-atom";
import { selectedAccountIdAtom } from "@/lib/atoms";
import { useFolderOrder } from "@/hooks/use-folder-order";

/**
 * Toolbar that appears above the mail list when mails are selected.
 * Provides bulk action buttons for move, archive, star, spam, delete.
 */
export function BulkToolbar() {
  const count = useAtomValue(selectionCountAtom);
  const accountId = useAtomValue(selectedAccountIdAtom);
  const bulkAction = useBulkAction();
  const clearSelection = useClearSelection();
  const { data: orderData } = useFolderOrder(accountId);

  if (count === 0 || !accountId) return null;

  const folders = orderData?.folders ?? [];

  return (
    <div className="flex items-center gap-2 border-b bg-muted/50 px-3 py-2">
      <Badge variant="secondary" className="mr-1">
        {count} selected
      </Badge>

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
                  body: {
                    action: "move",
                    target_folder_id: folder.folder_id,
                  },
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
        onClick={() =>
          bulkAction.mutate({ accountId, body: { action: "archive" } })
        }
      >
        <Archive className="h-3.5 w-3.5" />
        Archive
      </Button>

      <Button
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2"
        onClick={() =>
          bulkAction.mutate({ accountId, body: { action: "star" } })
        }
      >
        <Star className="h-3.5 w-3.5" />
        Star
      </Button>

      <Button
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2"
        onClick={() =>
          bulkAction.mutate({ accountId, body: { action: "spam" } })
        }
      >
        <Ban className="h-3.5 w-3.5" />
        Spam
      </Button>

      <Button
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2"
        onClick={() =>
          bulkAction.mutate({ accountId, body: { action: "delete" } })
        }
      >
        <Trash2 className="h-3.5 w-3.5" />
        Delete
      </Button>

      <div className="ml-auto">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2"
          onClick={() => clearSelection.mutate({ accountId })}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}
