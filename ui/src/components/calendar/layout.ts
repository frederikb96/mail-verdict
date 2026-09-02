/**
 * Pure layout functions for the calendar views: no React, no DOM, unit
 * testable on plain data. Every view (month, day/week, agenda, popover)
 * derives an event's visual state from `deriveEventLook` so they cannot
 * disagree about what an event is.
 */

import type { MouseEvent } from "react";
import type { EventInstance } from "@/types/api";

/** Shared by every view that can open the event popover -- the click event
 * is optional so a keyboard activation or a programmatic selection can omit
 * it, falling back to whatever anchor the caller already knows. */
export type SelectEventHandler = (
  objectId: string,
  recurrenceId: string | null,
  evt?: MouseEvent,
) => void;

/** Width of the month view's week-number gutter -- shared by the scroller's
 * header and each week row so the two stay aligned. */
export const WEEK_NUMBER_GUTTER_WIDTH = 28;

// --- Month view: spanning-bar lane assignment ---

export interface SpanningItem {
  key: string;
  /** Day index within the week, 0 (Monday) .. 6 (Sunday), inclusive. */
  startCol: number;
  endCol: number;
}

export interface LanedItem<T extends SpanningItem> {
  item: T;
  lane: number;
}

/**
 * Interval-graph colouring: sort by start then duration descending, and
 * greedily give each item the lowest lane whose current occupant has
 * already ended. Used for all-day/multi-day bars across a week row, and
 * reused for the day/week all-day tray.
 */
export function assignLanes<T extends SpanningItem>(items: T[]): LanedItem<T>[] {
  const sorted = [...items].sort((a, b) => {
    if (a.startCol !== b.startCol) return a.startCol - b.startCol;
    return b.endCol - b.startCol - (a.endCol - a.startCol);
  });

  // laneEnd[lane] = the endCol of the item currently occupying that lane.
  const laneEnd: number[] = [];
  const result: LanedItem<T>[] = [];

  for (const item of sorted) {
    let lane = laneEnd.findIndex((end) => end < item.startCol);
    if (lane === -1) {
      lane = laneEnd.length;
      laneEnd.push(item.endCol);
    } else {
      laneEnd[lane] = item.endCol;
    }
    result.push({ item, lane });
  }

  return result;
}

// --- Day/week view: overlap packing over a single day's timed events ---

export interface TimedItem {
  key: string;
  /** Minutes from midnight. */
  startMin: number;
  endMin: number;
}

export interface PackedItem<T extends TimedItem> {
  item: T;
  /** 0-indexed column within its overlap cluster. */
  column: number;
  /** Total columns in that cluster -- width is `1 / columns`. */
  columns: number;
  /** True when nothing occupies the column immediately to its right for the
   * item's whole duration, so it may visually expand into it. */
  expandable: boolean;
}

/**
 * Interval partitioning: walk events sorted by start, keeping the set of
 * currently-open events, and give each the lowest column index free at its
 * start. The cluster's column count is the high-water mark of concurrently
 * open events while any of its members were open.
 */
export function packColumns<T extends TimedItem>(items: T[]): PackedItem<T>[] {
  if (items.length === 0) return [];

  const sorted = [...items].sort((a, b) => a.startMin - b.startMin || a.endMin - b.endMin);

  // Cluster items that transitively overlap, so column counts are computed
  // per connected component rather than across the whole day.
  const clusters: T[][] = [];
  let current: T[] = [sorted[0]];
  let clusterMax = sorted[0].endMin;

  for (let i = 1; i < sorted.length; i++) {
    const item = sorted[i];
    if (item.startMin < clusterMax) {
      current.push(item);
      clusterMax = Math.max(clusterMax, item.endMin);
    } else {
      clusters.push(current);
      current = [item];
      clusterMax = item.endMin;
    }
  }
  clusters.push(current);

  const result: PackedItem<T>[] = [];

  for (const cluster of clusters) {
    // openUntil[col] = the endMin of the item currently holding that column.
    const openUntil: number[] = [];
    const assigned: { item: T; column: number }[] = [];

    for (const item of cluster) {
      let column = openUntil.findIndex((end) => end <= item.startMin);
      if (column === -1) {
        column = openUntil.length;
        openUntil.push(item.endMin);
      } else {
        openUntil[column] = item.endMin;
      }
      assigned.push({ item, column });
    }

    const columns = openUntil.length;

    for (const { item, column } of assigned) {
      const expandable = !cluster.some(
        (other) =>
          other !== item &&
          assigned.find((a) => a.item === other)!.column > column &&
          other.startMin < item.endMin &&
          item.startMin < other.endMin,
      );
      result.push({ item, column, columns, expandable });
    }
  }

  return result;
}

// --- Event visual state: one computation, every surface reads it ---

export type EventPresence = "solid" | "hollow" | "hatched" | "declined";

export interface EventLook {
  presence: EventPresence;
  cancelled: boolean;
  pending: boolean;
  failed: boolean;
  recurring: boolean;
  isException: boolean;
  replyNotSent: boolean;
  readOnly: boolean;
}

/** Derives every visual flag an event chip, grid block, agenda row or
 * popover needs from one instance, so they cannot disagree. */
export function deriveEventLook(instance: EventInstance): EventLook {
  const isOrganizer = instance.organizer === null;
  const partstat = instance.partstat;

  let presence: EventPresence = "solid";
  if (partstat === "declined") {
    presence = "declined";
  } else if (partstat === "tentative") {
    presence = "hatched";
  } else if (partstat === "needs-action" && !isOrganizer) {
    presence = "hollow";
  }

  const replyNotSent =
    partstat !== null &&
    partstat !== "needs-action" &&
    instance.own_reply !== null &&
    instance.own_reply.outbox_status === "dead";

  return {
    presence,
    cancelled: instance.status === "cancelled",
    pending: instance.pending,
    failed: instance.sync_error !== null,
    recurring: instance.is_recurring,
    isException: instance.is_exception,
    replyNotSent,
    readOnly: instance.read_only,
  };
}
