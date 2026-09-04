"use client";

import { useDraggable } from "@dnd-kit/core";
import { useAtomValue } from "jotai";
import { isRowSelected, selectionSize, type SelectableRow } from "@/lib/selection";
import { selectionAtom } from "@/store/selection-atom";

interface DragMailProps {
  row: SelectableRow;
  accountId?: string;
  folderId?: string;
  children: React.ReactNode;
}

/**
 * Draggable wrapper for a mail list item.
 *
 * A row that is part of the current multi-selection (predicate or
 * explicit) drags the whole selection -- the drop handler resolves it the
 * same way a bulk-action button would, since a predicate selection can
 * cover far more messages than are loaded to enumerate into a payload
 * here. A row outside the selection, or a selection of one, drags just
 * itself.
 */
export function DragMail({ row, accountId, folderId, children }: DragMailProps) {
  const selection = useAtomValue(selectionAtom);
  const size = selectionSize(selection);
  const isInSelection = size > 1 && isRowSelected(selection, row);

  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `mail-${row.id}`,
    data: {
      type: "mail",
      mailId: row.id,
      accountId,
      folderId,
      isSelectionDrag: isInSelection,
      count: isInSelection ? size : 1,
    },
  });

  return (
    <div
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      data-testid="mail-row"
      data-mail-id={row.id}
      className="relative"
      style={{ opacity: isDragging ? 0.5 : 1 }}
    >
      {children}
      {/* Drag count badge */}
      {isDragging && isInSelection && (
        <div className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-primary px-1 text-xs font-medium text-primary-foreground">
          {size}
        </div>
      )}
    </div>
  );
}
