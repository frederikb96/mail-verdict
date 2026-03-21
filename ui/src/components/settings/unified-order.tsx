"use client";

/**
 * Unified folder order: drag-and-drop reorderable list of unified folders.
 *
 * Only shows folders that have unified names assigned.
 */

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
import { Save, Loader2, GripVertical, Layers } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import {
  useUnifiedFolders,
  useUnifiedFolderOrder,
  useUpdateUnifiedFolderOrder,
} from "@/hooks/use-unified-view";
import type { UnifiedFolderResponse } from "@/types/api";

function SortableFolder({
  folder,
}: {
  folder: UnifiedFolderResponse;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: folder.unified_name });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex items-center gap-2 rounded-md border px-3 py-2 ${
        isDragging ? "opacity-50 shadow-lg" : ""
      }`}
    >
      <button
        className="cursor-grab touch-none"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="h-4 w-4 text-muted-foreground" />
      </button>
      <Layers className="h-4 w-4 text-muted-foreground" />
      <span className="flex-1 text-sm font-medium">
        {folder.unified_name}
      </span>
      <span className="text-xs text-muted-foreground">
        {folder.folders.length} account{folder.folders.length !== 1 ? "s" : ""}
      </span>
      {folder.unread_count > 0 && (
        <Badge variant="secondary" className="h-5 min-w-5 justify-center px-1 text-xs">
          {folder.unread_count}
        </Badge>
      )}
    </div>
  );
}

export function UnifiedOrder() {
  const { data: folders } = useUnifiedFolders();
  const { data: orderData } = useUnifiedFolderOrder();
  const updateOrder = useUpdateUnifiedFolderOrder();

  const [localOrder, setLocalOrder] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  // Build ordered folder list: stored order first, then unseen
  useEffect(() => {
    if (!folders) return;
    const names = folders.map((f) => f.unified_name);
    const storedOrder = orderData?.order ?? [];

    const ordered: string[] = [];
    const seen = new Set<string>();

    for (const name of storedOrder) {
      if (names.includes(name)) {
        ordered.push(name);
        seen.add(name);
      }
    }
    for (const name of names) {
      if (!seen.has(name)) {
        ordered.push(name);
      }
    }

    setLocalOrder(ordered);
    setDirty(false);
  }, [folders, orderData]);

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    const oldIndex = localOrder.indexOf(active.id as string);
    const newIndex = localOrder.indexOf(over.id as string);
    if (oldIndex === -1 || newIndex === -1) return;

    const updated = [...localOrder];
    updated.splice(oldIndex, 1);
    updated.splice(newIndex, 0, active.id as string);
    setLocalOrder(updated);
    setDirty(true);
  };

  const handleSave = () => {
    updateOrder.mutate(localOrder, {
      onSuccess: () => setDirty(false),
    });
  };

  // Build a map from unified name to response for rendering
  const folderMap = new Map<string, UnifiedFolderResponse>();
  folders?.forEach((f) => folderMap.set(f.unified_name, f));

  if (!folders?.length) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <Layers className="h-4 w-4" />
            Unified Folder Order
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            No unified folders configured. Assign unified names to folders first.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Layers className="h-4 w-4" />
          Unified Folder Order
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-xs text-muted-foreground">
          Drag folders to reorder the unified view sidebar.
        </p>

        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            items={localOrder}
            strategy={verticalListSortingStrategy}
          >
            <div className="flex flex-col gap-1">
              {localOrder.map((name) => {
                const folder = folderMap.get(name);
                if (!folder) return null;
                return (
                  <SortableFolder key={name} folder={folder} />
                );
              })}
            </div>
          </SortableContext>
        </DndContext>

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
