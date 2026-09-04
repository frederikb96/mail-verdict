"use client";

/**
 * Replaces the reading pane once more than one message is selected --
 * Outlook's own version of this panel, which is what Freddy pointed at.
 * A predicate ("select all") selection is server-resolved at action time
 * and never enumerated here, so a destructive action against one confirms
 * with the count rather than offering an undo it has no way to honour.
 */

import { useState } from "react";
import { Archive, Ban, ChevronDown, Mail as MailIcon, MailOpen, Trash2 } from "lucide-react";
import { useAtomValue } from "jotai";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useFolderOrder } from "@/hooks/use-folder-order";
import { useBulkAction, useSelection } from "@/hooks/use-selection";
import { useToast } from "@/hooks/use-toast";
import { selectedAccountIdAtom } from "@/lib/atoms";
import type { BulkActionType } from "@/types/api";

/** A batch is not atomic and cannot be undone -- these confirm with a
 * count and destination when the selection is a predicate rather than
 * offering an "Undo" that has nowhere to enumerate a source folder from. */
const DESTRUCTIVE_SCOPE_ACTIONS: BulkActionType[] = ["move", "trash", "spam", "expunge"];

interface PendingAction {
  action: BulkActionType;
  targetFolderId?: string;
  label: string;
}

export function BulkPanel() {
  const { count, state } = useSelection();
  const accountId = useAtomValue(selectedAccountIdAtom);
  const bulkAction = useBulkAction();
  const { data: orderData } = useFolderOrder(state.predicate?.accountId ?? accountId);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const { push: pushToast } = useToast();

  const folders = orderData?.folders ?? [];

  const execute = (action: BulkActionType, targetFolderId: string | undefined, label: string) => {
    // A predicate write is resolved as one statement over however many
    // rows match -- measured at tens of seconds for a large folder, all
    // of it server-side before the request even returns. Say so up front
    // rather than leaving the panel looking hung while it works.
    if (state.predicate) {
      pushToast(
        `Applying ${label.toLowerCase()} to ${count} messages -- this can take a while for a large selection.`,
        "info",
        6000,
      );
    }
    bulkAction.mutate({ action, targetFolderId });
  };

  const run = (action: BulkActionType, targetFolderId: string | undefined, label: string) => {
    if (state.predicate && DESTRUCTIVE_SCOPE_ACTIONS.includes(action)) {
      setPending({ action, targetFolderId, label });
      return;
    }
    execute(action, targetFolderId, label);
  };

  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 p-8">
      <Badge variant="secondary" className="text-sm">
        {count} message{count === 1 ? "" : "s"} selected
      </Badge>
      <div
        role="toolbar"
        aria-label="Bulk actions"
        className="flex flex-wrap items-center justify-center gap-2"
      >
        <Button variant="outline" size="sm" onClick={() => run("mark_read", undefined, "Mark as read")}>
          <MailOpen className="h-4 w-4" />
          Mark as read
        </Button>
        <Button variant="outline" size="sm" onClick={() => run("mark_unread", undefined, "Mark as unread")}>
          <MailIcon className="h-4 w-4" />
          Mark as unread
        </Button>
        <Button variant="outline" size="sm" onClick={() => run("archive", undefined, "Archive")}>
          <Archive className="h-4 w-4" />
          Archive
        </Button>
        <Button variant="outline" size="sm" onClick={() => run("spam", undefined, "Move to Junk")}>
          <Ban className="h-4 w-4" />
          Move to Junk
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="text-destructive"
          onClick={() => run("trash", undefined, "Move to trash")}
        >
          <Trash2 className="h-4 w-4" />
          Move to trash
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="outline" size="sm" className="gap-1" />}>
            Move to
            <ChevronDown className="h-3 w-3" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center" className="max-h-60 overflow-y-auto">
            {folders.map((folder) => (
              <DropdownMenuItem
                key={folder.folder_id}
                onClick={() => run("move", folder.folder_id, `Move to ${folder.imap_name}`)}
              >
                {folder.imap_name}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
        title={`${pending?.label ?? ""} ${count} messages?`}
        description="This acts on the whole selection as it stood when you selected it, resolved again at the moment you confirm. It cannot be undone."
        isConfirming={bulkAction.isPending}
        onConfirm={() => {
          if (!pending) return;
          execute(pending.action, pending.targetFolderId, pending.label);
          setPending(null);
        }}
      />
    </div>
  );
}
