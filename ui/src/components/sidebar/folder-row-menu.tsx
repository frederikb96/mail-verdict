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
import { useToast } from "@/hooks/use-toast";

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
  const { push: pushToast } = useToast();

  // A whole-folder write is resolved as one statement over however many
  // rows match -- measured at tens of seconds for a large folder, all of
  // it server-side before the request even returns. Said up front so the
  // menu doesn't look like it did nothing while it works.
  const warnIfSlow = (label: string, count: number) => {
    if (count > 0) {
      pushToast(
        `${label} ${count} message${count === 1 ? "" : "s"} -- this can take a while for a large folder.`,
        "info",
        6000,
      );
    }
  };

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
            onClick={() => {
              warnIfSlow("Marking", totalCount);
              folderAction.mutate({ accountId, folderId, action: "mark_read" });
            }}
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
        onConfirm={() => {
          warnIfSlow("Deleting", totalCount);
          folderAction.mutate(
            { accountId, folderId, action: "expunge" },
            { onSuccess: () => setConfirmEmpty(false) },
          );
        }}
      />
    </span>
  );
}
