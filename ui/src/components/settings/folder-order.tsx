"use client";

import { useState, useEffect } from "react";
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  Save,
  Loader2,
  Eye,
  EyeOff,
  Inbox,
  Send,
  Trash2,
  Archive,
  AlertTriangle,
  FileEdit,
  Folder,
  GripVertical,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  useFolderOrder,
  useUpdateFolderOrder,
  useToggleFolderVisibility,
} from "@/hooks/use-folder-order";
import type { FolderOrderItem } from "@/types/api";

const SPECIAL_USE_ICONS: Record<string, typeof Inbox> = {
  inbox: Inbox,
  sent: Send,
  trash: Trash2,
  archive: Archive,
  junk: AlertTriangle,
  drafts: FileEdit,
};

function getFolderIcon(specialUse: string | null) {
  if (specialUse && SPECIAL_USE_ICONS[specialUse]) {
    return SPECIAL_USE_ICONS[specialUse];
  }
  return Folder;
}

function SortableFolder({
  folder,
  onToggleVisibility,
}: {
  folder: FolderOrderItem;
  onToggleVisibility: (folderId: string, currentVisible: boolean) => void;
}) {
  const Icon = getFolderIcon(folder.special_use);
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: folder.folder_id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex items-center gap-2 px-3 py-1.5 ${
        isDragging ? "opacity-50" : ""
      }`}
    >
      <button
        className="cursor-grab touch-none"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="h-4 w-4 text-muted-foreground" />
      </button>
      <Icon className="h-4 w-4 text-muted-foreground" />
      <span
        className={`flex-1 text-sm ${!folder.is_visible ? "text-muted-foreground line-through" : ""}`}
      >
        {folder.imap_name}
      </span>
      {folder.unread_count > 0 && (
        <span className="text-xs text-muted-foreground">
          {folder.unread_count} unread
        </span>
      )}
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        onClick={() =>
          onToggleVisibility(folder.folder_id, folder.is_visible)
        }
        title={folder.is_visible ? "Hide folder" : "Show folder"}
      >
        {folder.is_visible ? (
          <Eye className="h-3.5 w-3.5" />
        ) : (
          <EyeOff className="h-3.5 w-3.5 text-muted-foreground" />
        )}
      </Button>
    </div>
  );
}

interface FolderOrderProps {
  accountId: string | null;
}

/**
 * Folder ordering via drag-and-drop and visibility toggle per folder.
 */
export function FolderOrder({ accountId }: FolderOrderProps) {
  const { data: orderData } = useFolderOrder(accountId);
  const updateOrder = useUpdateFolderOrder();
  const toggleVisibility = useToggleFolderVisibility();

  const [localFolders, setLocalFolders] = useState<FolderOrderItem[]>([]);
  const [dirty, setDirty] = useState(false);

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  useEffect(() => {
    if (orderData?.folders) {
      setLocalFolders(orderData.folders);
      setDirty(false);
    }
  }, [orderData]);

  if (!accountId) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <GripVertical className="h-4 w-4" />
            Folder Order
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Select an account to manage folder order
          </p>
        </CardContent>
      </Card>
    );
  }

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    const oldIndex = localFolders.findIndex(
      (f) => f.folder_id === active.id,
    );
    const newIndex = localFolders.findIndex(
      (f) => f.folder_id === over.id,
    );
    if (oldIndex === -1 || newIndex === -1) return;

    const updated = [...localFolders];
    updated.splice(oldIndex, 1);
    updated.splice(newIndex, 0, localFolders[oldIndex]);
    setLocalFolders(updated);
    setDirty(true);
  };

  const handleSave = () => {
    const order = localFolders.map((f) => f.folder_id);
    updateOrder.mutate(
      { accountId, order },
      { onSuccess: () => setDirty(false) },
    );
  };

  const handleToggleVisibility = (folderId: string, currentVisible: boolean) => {
    toggleVisibility.mutate({
      accountId,
      folderId,
      isVisible: !currentVisible,
    });
    setLocalFolders((prev) =>
      prev.map((f) =>
        f.folder_id === folderId ? { ...f, is_visible: !currentVisible } : f,
      ),
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <GripVertical className="h-4 w-4" />
          Folder Order & Visibility
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-xs text-muted-foreground">
          Drag folders to reorder. Hidden folders are excluded from the sidebar.
        </p>

        {localFolders.length === 0 && (
          <div className="py-4 text-sm text-muted-foreground">
            No folders available
          </div>
        )}

        {localFolders.length > 0 && (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragEnd={handleDragEnd}
          >
            <SortableContext
              items={localFolders.map((f) => f.folder_id)}
              strategy={verticalListSortingStrategy}
            >
              <div className="divide-y rounded-md border">
                {localFolders.map((folder) => (
                  <SortableFolder
                    key={folder.folder_id}
                    folder={folder}
                    onToggleVisibility={handleToggleVisibility}
                  />
                ))}
              </div>
            </SortableContext>
          </DndContext>
        )}

        {dirty && (
          <div className="mt-4 flex justify-end">
            <Button
              onClick={handleSave}
              disabled={updateOrder.isPending}
              size="sm"
            >
              {updateOrder.isPending ? (
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
              ) : (
                <Save className="mr-1 h-3 w-3" />
              )}
              Save Order
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
