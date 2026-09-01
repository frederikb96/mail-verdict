"use client";

import { useDroppable } from "@dnd-kit/core";
import { cn } from "@/lib/utils";

interface FolderMapping {
  account_id: string;
  folder_id: string;
}

interface DroppableFolderProps {
  folderId: string;
  folderMapping?: FolderMapping[];
  children: React.ReactNode;
}

/**
 * Droppable wrapper for a sidebar folder item.
 * Supports both single-account folders and unified folders with per-account mapping.
 */
export function DroppableFolder({ folderId, folderMapping, children }: DroppableFolderProps) {
  const { isOver, setNodeRef } = useDroppable({
    id: `folder-${folderId}`,
    data: {
      type: "folder",
      folderId,
      folderMapping,
    },
  });

  return (
    <div
      ref={setNodeRef}
      data-testid="folder"
      data-folder-id={folderId}
      className={cn(
        "transition-colors",
        isOver && "rounded-md ring-2 ring-primary/50 bg-primary/10",
      )}
    >
      {children}
    </div>
  );
}
