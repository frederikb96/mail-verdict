"use client";

import { useDroppable } from "@dnd-kit/core";
import { cn } from "@/lib/utils";

interface DroppableFolderProps {
  folderId: string;
  children: React.ReactNode;
}

/**
 * Droppable wrapper for a sidebar folder item.
 * Highlights when a valid mail drag hovers over it.
 */
export function DroppableFolder({ folderId, children }: DroppableFolderProps) {
  const { isOver, setNodeRef } = useDroppable({
    id: `folder-${folderId}`,
    data: {
      type: "folder",
      folderId,
    },
  });

  return (
    <div
      ref={setNodeRef}
      className={cn(
        "transition-colors",
        isOver && "rounded-md ring-2 ring-primary/50 bg-primary/10",
      )}
    >
      {children}
    </div>
  );
}
