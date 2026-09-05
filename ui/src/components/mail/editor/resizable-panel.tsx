"use client";

import { useCallback, useRef, useState } from "react";
import { Maximize2, Minimize2 } from "lucide-react";

import { Button } from "@/components/ui/button";

const MIN_HEIGHT_PX = 160;
// Roughly what the editor's previous fixed 40vh/50vh caps worked out to
// at an ordinary window size -- a starting point for the drag, not a
// value anything else depends on.
const DEFAULT_HEIGHT_PX = 260;
// Left clear at the bottom of the viewport for whatever sits below the
// editor (recipient fields, the Send row) when dragged to its tallest.
const VIEWPORT_MARGIN_PX = 220;

export interface ComposeResizeControls {
  heightPx: number;
  isMaximized: boolean;
  toggleMaximized: () => void;
  dragHandleProps: { onPointerDown: (event: React.PointerEvent) => void };
}

/**
 * Drag-to-resize height plus a maximize toggle for a compose surface --
 * shared by the reply box and the new-message dialog (compose-form.tsx),
 * each translating `isMaximized` into whatever "fill the window" means
 * for its own layout.
 */
export function useComposeResize(): ComposeResizeControls {
  const [heightPx, setHeightPx] = useState(DEFAULT_HEIGHT_PX);
  const [isMaximized, setIsMaximized] = useState(false);
  const dragState = useRef<{ startY: number; startHeight: number } | null>(null);

  const onPointerMove = useCallback((event: PointerEvent) => {
    if (!dragState.current) return;
    // Dragging the top edge upward (clientY decreasing) grows the panel,
    // since it is anchored at the bottom of its own container in both
    // hosts this is used from.
    const delta = dragState.current.startY - event.clientY;
    const max = window.innerHeight - VIEWPORT_MARGIN_PX;
    setHeightPx(Math.min(Math.max(dragState.current.startHeight + delta, MIN_HEIGHT_PX), max));
  }, []);

  const onPointerUp = useCallback(() => {
    dragState.current = null;
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
  }, [onPointerMove]);

  const onPointerDown = useCallback(
    (event: React.PointerEvent) => {
      if (isMaximized) return;
      dragState.current = { startY: event.clientY, startHeight: heightPx };
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", onPointerUp);
    },
    [heightPx, isMaximized, onPointerMove, onPointerUp],
  );

  const toggleMaximized = useCallback(() => setIsMaximized((v) => !v), []);

  return { heightPx, isMaximized, toggleMaximized, dragHandleProps: { onPointerDown } };
}

interface ComposeResizeControlsBarProps {
  controls: ComposeResizeControls;
}

/** The drag handle plus the maximize/restore button, one row above the editor. */
export function ComposeResizeControlsBar({ controls }: ComposeResizeControlsBarProps) {
  return (
    <div className="flex items-center justify-center gap-2 pb-1">
      {!controls.isMaximized && (
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize message body"
          className="h-1.5 w-10 shrink-0 cursor-row-resize touch-none rounded-full bg-border hover:bg-muted-foreground/50"
          {...controls.dragHandleProps}
        />
      )}
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="ml-auto"
        title={controls.isMaximized ? "Restore" : "Expand to full screen"}
        onClick={controls.toggleMaximized}
      >
        {controls.isMaximized ? <Minimize2 /> : <Maximize2 />}
      </Button>
    </div>
  );
}
