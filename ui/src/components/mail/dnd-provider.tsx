"use client";

import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { useState } from "react";
import { useAtomValue } from "jotai";
import { GripVertical } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useBulkAction } from "@/hooks/use-selection";
import { useMailAction } from "@/hooks/use-mails";
import { selectedAccountIdAtom } from "@/lib/atoms";
import { selectedMailIdsAtom } from "@/store/selection-atom";

interface MailDndProviderProps {
  children: React.ReactNode;
}

/**
 * Top-level DndContext wrapper for mail drag-and-drop.
 * Handles onDragEnd to dispatch move actions when mails are dropped on folders.
 */
export function MailDndProvider({ children }: MailDndProviderProps) {
  const accountId = useAtomValue(selectedAccountIdAtom);
  const selectedIds = useAtomValue(selectedMailIdsAtom);
  const bulkAction = useBulkAction();
  const mailAction = useMailAction();
  const [dragData, setDragData] = useState<{
    count: number;
    mailIds: string[];
  } | null>(null);

  // Require 5px movement before starting drag (prevents click interference)
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
    if (!over || !accountId) return;

    const activeData = active.data.current;
    const overData = over.data.current;

    if (activeData?.type !== "mail" || overData?.type !== "folder") return;

    const targetFolderId = overData.folderId as string;
    const mailIds = activeData.mailIds as string[];

    if (mailIds.length > 1) {
      // Multi-drag: use bulk action
      bulkAction.mutate({
        accountId,
        body: {
          action: "move",
          target_folder_id: targetFolderId,
        },
      });
    } else if (mailIds.length === 1) {
      // Single drag: use individual mail action
      // Need to resolve folder name — use the folder_id directly via move
      // The backend move action accepts target_folder (imap name),
      // but we have folder_id. Use bulk action for consistency.
      bulkAction.mutate({
        accountId,
        body: {
          action: "move",
          target_folder_id: targetFolderId,
        },
      });
    }
  }

  return (
    <DndContext
      sensors={sensors}
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
