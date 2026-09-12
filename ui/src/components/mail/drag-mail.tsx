"use client";

import { useRef, useState } from "react";
import { useDraggable } from "@dnd-kit/core";
import { useAtomValue } from "jotai";
import { isRowSelected, selectionSize, type SelectableRow } from "@/lib/selection";
import { effectiveSelectionAtom } from "@/store/selection-atom";

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
  // The effective selection, not the raw one -- a selection made in a
  // folder the reader has since left must not drag as "the whole
  // selection" here either, the same guard bulk actions apply.
  const selection = useAtomValue(effectiveSelectionAtom);
  const size = selectionSize(selection);
  const isInSelection = size > 1 && isRowSelected(selection, row);

  // A hand-rolled equivalent of :focus-visible, scoped to exactly this
  // question: was this focus caused by a pointer press, or something
  // else (a real Tab press, or a program calling .focus() directly, e.g.
  // an accessibility test)? CSS :focus-visible cannot answer that for a
  // *programmatic* .focus() call the way this row's own test suite
  // exercises it -- Chromium's own heuristic there generally does not
  // grant focus-visible, which silently broke keyboard-reachability
  // coverage the one time this was tried. Capture phase, a distinct prop
  // name from dnd-kit's own `onPointerDown` in `listeners` below, so
  // nothing here overrides its drag handling.
  const pointerDownRef = useRef(false);
  const [keyboardFocused, setKeyboardFocused] = useState(false);

  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `mail-${row.id}`,
    data: {
      type: "mail",
      mailId: row.id,
      accountId,
      folderId,
      isSelectionDrag: isInSelection,
      count: isInSelection ? size : 1,
      // Carried so a touch long-press (handled in MailDndProvider, which
      // has no other way to reach this row's own fields) can select it
      // without dragging -- see the touch handling there.
      isSeen: row.is_seen,
      mirroredAt: row.mirrored_at,
    },
  });

  return (
    <div
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      data-testid="mail-row"
      data-mail-id={row.id}
      // Named group, not the bare "group" MailListItem/UnifiedMailItem
      // used to declare on their own root -- dnd-kit's own `attributes`
      // above already puts tabIndex on *this* element for keyboard-driven
      // dragging, so this is the row's one real tab stop. Keying the
      // hover-reveal on a second, inner tabIndex would have made
      // keyboard focus land on a stop that reveals nothing, one Tab
      // before the one that does.
      className="group/row relative"
      style={{ opacity: isDragging ? 0.5 : 1 }}
      data-kbd-focus={keyboardFocused ? "" : undefined}
      onPointerDownCapture={() => {
        pointerDownRef.current = true;
        // Only suppresses the focus event this same press causes,
        // synchronously right after -- a later, unrelated focus (a real
        // Tab press, say) must not read a stale flag from an earlier click
        // that never actually focused this row.
        window.setTimeout(() => {
          pointerDownRef.current = false;
        }, 0);
      }}
      onFocus={() => {
        if (!pointerDownRef.current) setKeyboardFocused(true);
      }}
      onBlur={() => setKeyboardFocused(false)}
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
