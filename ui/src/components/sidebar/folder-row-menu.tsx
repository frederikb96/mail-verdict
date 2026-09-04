"use client";

/**
 * Sits where a folder row's unread badge normally shows, swapping to a
 * three-dot control on hover -- Freddy pointed at this exact Outlook
 * pattern. Rendered as a sibling of the folder's own SidebarMenuButton,
 * never nested inside it: a dropdown trigger button cannot live inside
 * another button.
 */

import { useState } from "react";
import { MoreVertical } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useFolderBulkAction } from "@/hooks/use-selection";

interface FolderRowMenuProps {
  accountId: string;
  folderId: string;
  folderName: string;
  badgeCount: number;
  totalCount: number;
}

export function FolderRowMenu({
  accountId, folderId, folderName, badgeCount, totalCount,
}: FolderRowMenuProps) {
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const folderAction = useFolderBulkAction();

  return (
    <span
      className="ml-auto flex h-5 shrink-0 items-center"
      onClick={(e) => e.stopPropagation()}
    >
      {badgeCount > 0 && (
        <Badge
          variant="secondary"
          className="h-5 min-w-5 justify-center px-1 text-xs group-hover/menu-item:hidden"
        >
          {badgeCount}
        </Badge>
      )}
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              variant="ghost"
              size="icon-xs"
              className="hidden group-hover/menu-item:flex"
            />
          }
          title={`${folderName} options`}
          aria-label={`${folderName} options`}
        >
          <MoreVertical className="h-3.5 w-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            onClick={() => folderAction.mutate({ accountId, folderId, action: "mark_read" })}
          >
            Mark all as read
          </DropdownMenuItem>
          <DropdownMenuItem
            variant="destructive"
            onClick={() => setConfirmEmpty(true)}
          >
            Empty folder
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <ConfirmDialog
        open={confirmEmpty}
        onOpenChange={setConfirmEmpty}
        title={`Empty ${folderName}?`}
        confirmLabel="Empty folder"
        description={
          `This permanently deletes ${totalCount} message${totalCount === 1 ? "" : "s"} ` +
          "from the mail server. It cannot be undone."
        }
        isConfirming={folderAction.isPending}
        onConfirm={() =>
          folderAction.mutate(
            { accountId, folderId, action: "expunge" },
            { onSuccess: () => setConfirmEmpty(false) },
          )
        }
      />
    </span>
  );
}
