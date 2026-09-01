"use client";

import { useDraggable } from "@dnd-kit/core";
import { useAtomValue } from "jotai";
import { selectedMailIdsAtom, selectionModeAtom } from "@/store/selection-atom";

interface DragMailProps {
  mailId: string;
  accountId?: string;
  folderId?: string;
  children: React.ReactNode;
}

/**
 * Draggable wrapper for a mail list item.
 * When dragging a selected mail, all selected mails move together.
 */
export function DragMail({ mailId, accountId, folderId, children }: DragMailProps) {
  const selectedIds = useAtomValue(selectedMailIdsAtom);
  const selectionMode = useAtomValue(selectionModeAtom);
  const isInSelection = selectedIds.has(mailId);

  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `mail-${mailId}`,
    data: {
      type: "mail",
      mailId,
      accountId,
      folderId,
      mailIds: isInSelection ? Array.from(selectedIds) : [mailId],
      count: isInSelection ? selectedIds.size : 1,
    },
  });

  return (
    <div
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      data-testid="mail-row"
      data-mail-id={mailId}
      className="relative"
      style={{ opacity: isDragging ? 0.5 : 1 }}
    >
      {children}
      {/* Drag count badge */}
      {isDragging && selectionMode && isInSelection && selectedIds.size > 1 && (
        <div className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-primary px-1 text-xs font-medium text-primary-foreground">
          {selectedIds.size}
        </div>
      )}
    </div>
  );
}
