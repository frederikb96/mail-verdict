"use client";

import {
  DndContext,
  DragOverlay,
  MouseSensor,
  TouchSensor,
  pointerWithin,
  useSensor,
  useSensors,
  type Active,
  type Announcements,
  type Over,
  type DragCancelEvent,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { useRef, useState } from "react";
import { useAtomValue } from "jotai";
import { GripVertical } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useMailAction } from "@/hooks/use-mails";
import { useBulkAction, useSelectionGestures } from "@/hooks/use-selection";
import { selectedAccountIdAtom, isUnifiedViewAtom } from "@/lib/atoms";

interface MailDndProviderProps {
  children: React.ReactNode;
}

/**
 * Top-level DndContext wrapper for mail drag-and-drop.
 * Supports both single-account and unified view with per-account folder mapping.
 */
export function MailDndProvider({ children }: MailDndProviderProps) {
  const accountId = useAtomValue(selectedAccountIdAtom);
  const isUnified = useAtomValue(isUnifiedViewAtom);
  const mailAction = useMailAction();
  const bulkAction = useBulkAction();
  const { toggle } = useSelectionGestures();
  const [dragData, setDragData] = useState<{
    count: number;
  } | null>(null);
  // Set for the whole lifetime of a touch-activated drag -- read in
  // onDragEnd/onDragCancel to skip the move entirely, since touch never
  // drags to move (see handleDragStart below).
  const touchSelectRef = useRef(false);

  // Mouse keeps today's distance-based activation exactly as it was.
  // Touch gets its own, time-based one: a quick swipe exceeds the 5px
  // tolerance well before the 250ms delay elapses, so it cancels
  // activation and the browser's native scroll takes over untouched --
  // that's what stops a scroll gesture from ever picking a message up.
  // A touch that stays still past the delay *is* a long press, which
  // handleDragStart below turns into "select this row" rather than a drag.
  const sensors = useSensors(
    useSensor(MouseSensor, {
      activationConstraint: {
        distance: 5,
      },
    }),
    useSensor(TouchSensor, {
      activationConstraint: {
        delay: 250,
        tolerance: 5,
      },
    }),
  );

  function handleDragStart(event: DragStartEvent) {
    const data = event.active.data.current;
    if (data?.type !== "mail") return;

    if (event.activatorEvent instanceof TouchEvent) {
      // A long press: select the row under the finger (unless it's
      // already part of a multi-selection being pressed on, which stays
      // as it is) and never show a drag ghost or perform a move -- touch
      // long-press is "enter selection", not "start dragging".
      touchSelectRef.current = true;
      if (!data.isSelectionDrag) {
        toggle({
          id: data.mailId as string,
          account_id: data.accountId as string,
          folder_id: data.folderId as string,
          is_seen: data.isSeen as boolean,
          mirrored_at: data.mirroredAt as string | undefined,
        });
      }
      return;
    }

    touchSelectRef.current = false;
    setDragData({ count: data.count as number });
  }

  function handleDragCancel(_event: DragCancelEvent) {
    touchSelectRef.current = false;
    setDragData(null);
  }

  function handleDragEnd(event: DragEndEvent) {
    setDragData(null);
    if (touchSelectRef.current) {
      touchSelectRef.current = false;
      return;
    }

    const { active, over } = event;
    if (!over) return;

    const activeData = active.data.current;
    const overData = over.data.current;

    if (activeData?.type !== "mail" || overData?.type !== "folder") return;

    const mailAccountId = activeData.accountId as string | undefined;
    const folderMapping = overData.folderMapping as
      | { account_id: string; folder_id: string }[]
      | undefined;
    const dropFolderId = overData.folderId as string;

    // Dropping back onto the folder the message is already in is a no-op --
    // skip the request rather than sending a move with no destination change.
    const sourceFolderId = activeData.folderId as string | undefined;
    if (sourceFolderId && sourceFolderId === dropFolderId) return;

    // A unified folder maps to a different id per account -- resolve each
    // account's own id for it rather than assuming the one the pointer
    // happens to be over applies everywhere.
    const targetFolderIdForAccount = (forAccountId: string): string | undefined => {
      if (!isUnified) return dropFolderId;
      const match = folderMapping?.find((f) => f.account_id === forAccountId);
      return match?.folder_id;
    };

    if (activeData.isSelectionDrag) {
      bulkAction.mutate({
        action: "move",
        targetFolderId: isUnified ? targetFolderIdForAccount : dropFolderId,
      });
      return;
    }

    const mailId = activeData.mailId as string;
    const effectiveAccountId = isUnified ? mailAccountId : accountId;
    const targetFolderId = isUnified
      ? (mailAccountId && targetFolderIdForAccount(mailAccountId))
      : dropFolderId;
    if (!effectiveAccountId || !targetFolderId) return;

    mailAction.mutate({
      mailId,
      accountId: effectiveAccountId,
      action: { action: "move", target_folder_id: targetFolderId },
    });
  }

  // dnd-kit's default announcements read out its internal draggable/droppable
  // ids ("Draggable item mail-<uuid> was dropped over droppable area
  // folder-<uuid>"). Replace them with the message count and folder name a
  // screen reader user actually needs.
  const mailLabel = (count: number) => (count === 1 ? "1 message" : `${count} messages`);
  const activeCount = (active: Active) => (active.data.current?.count as number | undefined) ?? 1;
  const folderLabel = (over: Over) =>
    (over.data.current?.folderName as string | undefined) ?? "the folder";

  const announcements: Announcements = {
    onDragStart: ({ active }) => `Picked up ${mailLabel(activeCount(active))}.`,
    onDragOver: ({ active, over }) =>
      over ? `${mailLabel(activeCount(active))} is over ${folderLabel(over)}.` : undefined,
    onDragEnd: ({ active, over }) =>
      over
        ? `${mailLabel(activeCount(active))} moved to ${folderLabel(over)}.`
        : `${mailLabel(activeCount(active))} dropped, no folder targeted.`,
    onDragCancel: ({ active }) => `Moving ${mailLabel(activeCount(active))} cancelled.`,
  };

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={pointerWithin}
      accessibility={{ announcements }}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
      onDragCancel={handleDragCancel}
    >
      {children}
      <DragOverlay>
        {dragData && (
          <div className="flex items-center gap-2 rounded-md border bg-background px-3 py-2 shadow-lg">
            <GripVertical className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm">
              {dragData.count === 1
                ? "1 message"
                : `${dragData.count} messages`}
            </span>
            {dragData.count > 1 && (
              <Badge variant="secondary" className="ml-1">
                {dragData.count}
              </Badge>
            )}
          </div>
        )}
      </DragOverlay>
    </DndContext>
  );
}
