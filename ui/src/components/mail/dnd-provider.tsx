"use client";

import {
  DndContext,
  DragOverlay,
  PointerSensor,
  pointerWithin,
  useSensor,
  useSensors,
  type Active,
  type Announcements,
  type Over,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { useState } from "react";
import { useAtomValue } from "jotai";
import { GripVertical } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useMailAction } from "@/hooks/use-mails";
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
  const [dragData, setDragData] = useState<{
    count: number;
    mailIds: string[];
  } | null>(null);

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: {
        distance: 5,
      },
    }),
  );

  function handleDragStart(event: DragStartEvent) {
    const data = event.active.data.current;
    if (data?.type === "mail") {
      setDragData({
        count: data.count as number,
        mailIds: data.mailIds as string[],
      });
    }
  }

  function handleDragEnd(event: DragEndEvent) {
    setDragData(null);

    const { active, over } = event;
    if (!over) return;

    const activeData = active.data.current;
    const overData = over.data.current;

    if (activeData?.type !== "mail" || overData?.type !== "folder") return;

    const mailIds = activeData.mailIds as string[];
    const mailAccountId = activeData.accountId as string | undefined;
    const folderMapping = overData.folderMapping as
      | { account_id: string; folder_id: string }[]
      | undefined;

    let targetFolderId = overData.folderId as string;
    let effectiveAccountId = isUnified ? mailAccountId : accountId;

    if (isUnified && folderMapping && mailAccountId) {
      const match = folderMapping.find((f) => f.account_id === mailAccountId);
      if (match) {
        targetFolderId = match.folder_id;
      }
    }

    if (!effectiveAccountId) return;

    // Dropping back onto the folder the message is already in is a no-op --
    // skip the request rather than sending a move with no destination change.
    const sourceFolderId = activeData.folderId as string | undefined;
    if (sourceFolderId && sourceFolderId === targetFolderId) return;

    for (const mailId of mailIds) {
      mailAction.mutate({
        mailId,
        accountId: effectiveAccountId,
        action: { action: "move", target_folder_id: targetFolderId },
      });
    }
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
