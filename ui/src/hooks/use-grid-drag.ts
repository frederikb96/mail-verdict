"use client";

/**
 * Move, resize and create-by-drag on the day/week time grid, driven by
 * pointer events rather than @dnd-kit -- a time grid is a continuous
 * coordinate transform with snapping and no droppables, and modelling every
 * 15-minute slot as a droppable is the wrong shape for that.
 *
 * The ghost lives in React state, not a DOM mutation, and the real chip
 * stays in place at reduced opacity while it is dragged -- nothing waits on
 * the server for the drag itself to feel immediate.
 */

import { useCallback, useRef, useState } from "react";

export const SNAP_MINUTES = 15;
export const MINUTES_PER_DAY = 24 * 60;

export interface GridGhost {
  objectId: string;
  recurrenceId: string | null;
  /** Minutes from midnight, day 0 of the columns passed to the hook. */
  startMin: number;
  endMin: number;
  /** Which day column the ghost is currently over (0-indexed). */
  column: number;
  kind: "move" | "resize-start" | "resize-end" | "create";
}

interface UseGridDragOptions {
  columns: number;
  pixelsPerMinute: number;
  onCommitMove?: (ghost: GridGhost) => void;
  onCommitCreate?: (ghost: GridGhost) => void;
}

function snap(minutes: number): number {
  return Math.round(minutes / SNAP_MINUTES) * SNAP_MINUTES;
}

export function useGridDrag({ columns, pixelsPerMinute, onCommitMove, onCommitCreate }: UseGridDragOptions) {
  const [ghost, setGhost] = useState<GridGhost | null>(null);
  const originRef = useRef<{
    startMin: number;
    endMin: number;
    pointerStartMin: number;
    column: number;
  } | null>(null);

  const minutesFromClientY = useCallback(
    (containerTop: number, clientY: number) => (clientY - containerTop) / pixelsPerMinute,
    [pixelsPerMinute],
  );

  const columnFromClientX = useCallback(
    (containerLeft: number, containerWidth: number, clientX: number) => {
      const fraction = (clientX - containerLeft) / containerWidth;
      return Math.min(columns - 1, Math.max(0, Math.floor(fraction * columns)));
    },
    [columns],
  );

  const beginMove = useCallback(
    (
      e: React.PointerEvent,
      objectId: string,
      recurrenceId: string | null,
      startMin: number,
      endMin: number,
      column: number,
      kind: "move" | "resize-start" | "resize-end",
    ) => {
      e.currentTarget.setPointerCapture(e.pointerId);
      const container = (e.currentTarget as HTMLElement).closest("[data-grid-surface]") as HTMLElement | null;
      const containerRect = container?.getBoundingClientRect();
      const pointerStartMin = containerRect ? minutesFromClientY(containerRect.top, e.clientY) : startMin;
      originRef.current = { startMin, endMin, pointerStartMin, column };
      setGhost({ objectId, recurrenceId, startMin, endMin, column, kind });
    },
    [minutesFromClientY],
  );

  const beginCreate = useCallback(
    (containerTop: number, clientY: number, column: number) => {
      const min = snap(minutesFromClientY(containerTop, clientY));
      originRef.current = { startMin: min, endMin: min, pointerStartMin: min, column };
      setGhost({
        objectId: "__new__",
        recurrenceId: null,
        startMin: min,
        endMin: min + SNAP_MINUTES,
        column,
        kind: "create",
      });
    },
    [minutesFromClientY],
  );

  const updateDrag = useCallback(
    (containerRect: DOMRect, clientX: number, clientY: number) => {
      const origin = originRef.current;
      if (!origin || !ghost) return;

      const pointerMin = minutesFromClientY(containerRect.top, clientY);
      const column =
        ghost.kind === "move" || ghost.kind === "create"
          ? columnFromClientX(containerRect.left, containerRect.width, clientX)
          : ghost.column;

      if (ghost.kind === "move") {
        const delta = snap(pointerMin - origin.pointerStartMin);
        const duration = origin.endMin - origin.startMin;
        const startMin = Math.max(0, Math.min(MINUTES_PER_DAY - duration, origin.startMin + delta));
        setGhost((g) => (g ? { ...g, startMin, endMin: startMin + duration, column } : g));
      } else if (ghost.kind === "resize-start") {
        const startMin = Math.min(origin.endMin - SNAP_MINUTES, Math.max(0, snap(pointerMin)));
        setGhost((g) => (g ? { ...g, startMin } : g));
      } else if (ghost.kind === "resize-end") {
        const endMin = Math.max(origin.startMin + SNAP_MINUTES, Math.min(MINUTES_PER_DAY, snap(pointerMin)));
        setGhost((g) => (g ? { ...g, endMin } : g));
      } else if (ghost.kind === "create") {
        const current = snap(pointerMin);
        const startMin = Math.min(origin.startMin, current);
        const endMin = Math.max(origin.startMin + SNAP_MINUTES, current);
        setGhost((g) => (g ? { ...g, startMin, endMin, column } : g));
      }
    },
    [ghost, minutesFromClientY, columnFromClientX],
  );

  const commit = useCallback(() => {
    const origin = originRef.current;
    if (ghost && origin) {
      if (ghost.kind === "create") {
        onCommitCreate?.(ghost);
      } else {
        // A release at the same slot and column it started from is a click,
        // not a drag -- committing it anyway would write on every click.
        const moved =
          ghost.startMin !== origin.startMin ||
          ghost.endMin !== origin.endMin ||
          ghost.column !== origin.column;
        if (moved) onCommitMove?.(ghost);
      }
    }
    setGhost(null);
    originRef.current = null;
  }, [ghost, onCommitCreate, onCommitMove]);

  const cancel = useCallback(() => {
    setGhost(null);
    originRef.current = null;
  }, []);

  return { ghost, beginMove, beginCreate, updateDrag, commit, cancel };
}
