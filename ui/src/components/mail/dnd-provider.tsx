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
import { useMailAction } from "@/hooks/use-mails";
import { selectedAccountIdAtom } from "@/lib/atoms";

interface MailDndProviderProps {
  children: React.ReactNode;
}

/**
 * Top-level DndContext wrapper for mail drag-and-drop.
 * Uses individual mailAction (move by folder_id) for each dragged mail.
 */
export function MailDndProvider({ children }: MailDndProviderProps) {
  const accountId = useAtomValue(selectedAccountIdAtom);
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
    if (!over || !accountId) return;

    const activeData = active.data.current;
    const overData = over.data.current;

    if (activeData?.type !== "mail" || overData?.type !== "folder") return;

    const targetFolderId = overData.folderId as string;
    const mailIds = activeData.mailIds as string[];

    for (const mailId of mailIds) {
      mailAction.mutate({
        mailId,
        accountId,
        action: { action: "move", target_folder_id: targetFolderId },
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
