"use client";

/**
 * The one component that renders an event. Every view -- month cell, grid
 * block, agenda row, spanning bar -- uses this with a different `variant`,
 * reading its visual state from `deriveEventLook` so no view can disagree
 * about what an event looks like.
 */

import type { CSSProperties, MouseEvent, PointerEvent } from "react";
import { AlertTriangle, Ban, Loader2, Repeat } from "lucide-react";
import { cn } from "@/lib/utils";
import { deriveEventLook } from "@/components/calendar/layout";
import { resolveCalendarColor } from "@/components/calendar/colors";
import type { Calendar, EventInstance } from "@/types/api";

export type EventChipVariant = "month" | "grid" | "agenda" | "allday";

interface EventChipProps {
  event: EventInstance;
  calendar: Calendar | undefined;
  variant: EventChipVariant;
  timeLabel?: string;
  selected?: boolean;
  onClick?: (e: MouseEvent) => void;
  onPointerDown?: (e: PointerEvent) => void;
  style?: CSSProperties;
  className?: string;
}

export function EventChip({
  event,
  calendar,
  variant,
  timeLabel,
  selected,
  onClick,
  onPointerDown,
  style,
  className,
}: EventChipProps) {
  const look = deriveEventLook(event);
  const color = calendar ? resolveCalendarColor(calendar) : "var(--muted-foreground)";
  const effectiveColor = look.cancelled ? "var(--muted-foreground)" : color;

  const chipStyle: CSSProperties = {
    ...style,
    borderLeftColor: look.presence !== "hollow" ? effectiveColor : undefined,
    borderColor: look.presence === "hollow" ? effectiveColor : undefined,
    color: `color-mix(in oklab, ${effectiveColor} 65%, var(--foreground))`,
  };
  (chipStyle as Record<string, string>)["--cal-color"] = effectiveColor;

  return (
    <div
      data-testid="event"
      data-event-id={event.object_id}
      data-recurrence-id={event.recurrence_id ?? ""}
      role="button"
      tabIndex={0}
      onClick={onClick}
      onPointerDown={onPointerDown}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onClick?.(e as unknown as MouseEvent);
      }}
      style={chipStyle}
      className={cn(
        "group/chip flex min-w-0 cursor-pointer items-center gap-1 overflow-hidden rounded px-1 text-xs leading-tight outline-none transition-opacity",
        "focus-visible:ring-2 focus-visible:ring-ring",
        variant === "month" && "h-[18px]",
        variant === "grid" && "absolute h-full rounded-md px-1.5 py-0.5 text-left",
        variant === "agenda" && "h-8 rounded-md px-2 py-1",
        variant === "allday" && "h-[18px] rounded",
        look.presence === "solid" &&
          "border-l-2 bg-[color-mix(in_oklab,var(--cal-color)_18%,transparent)]",
        look.presence === "hatched" &&
          "border-l-2 bg-[repeating-linear-gradient(135deg,color-mix(in_oklab,var(--cal-color)_22%,transparent)_0,color-mix(in_oklab,var(--cal-color)_22%,transparent)_4px,transparent_4px,transparent_8px)]",
        look.presence === "hollow" && "border bg-transparent",
        look.presence === "declined" &&
          "border-l-2 bg-[color-mix(in_oklab,var(--cal-color)_8%,transparent)] line-through opacity-50",
        look.cancelled && "text-muted-foreground line-through",
        look.pending && "opacity-60",
        look.failed && "border-l-2 border-destructive",
        selected && "ring-2 ring-ring",
        className,
      )}
    >
      {look.pending && <Loader2 className="h-3 w-3 shrink-0 animate-spin" />}
      {look.failed && !look.pending && (
        <AlertTriangle className="h-3 w-3 shrink-0 text-destructive" />
      )}
      {look.replyNotSent && !look.failed && (
        <AlertTriangle className="h-3 w-3 shrink-0 text-amber-500" />
      )}
      {look.cancelled && <Ban className="h-3 w-3 shrink-0" />}
      {look.recurring && !look.cancelled && (
        <Repeat className="h-2.5 w-2.5 shrink-0 opacity-70" />
      )}
      {timeLabel && variant !== "allday" && (
        <span className="shrink-0 font-medium opacity-80">{timeLabel}</span>
      )}
      <span className="truncate">{event.summary || "(no title)"}</span>
    </div>
  );
}
