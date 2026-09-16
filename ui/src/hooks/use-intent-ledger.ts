/** React access to the mail intent ledger and the state of its sending. */

import { useMemo, useSyncExternalStore } from "react";
import {
  getLedgerSnapshot,
  getServerLedgerSnapshot,
  subscribeLedger,
  type LedgerSnapshot,
} from "@/lib/intent-ledger";
import {
  getDrainerStatus,
  getServerDrainerStatus,
  subscribeDrainerStatus,
  type DrainerStatus,
} from "@/lib/intent-drainer";
import { folderCountDeltas, projectCounts } from "@/lib/mail-intents";

export function useIntentLedger(): LedgerSnapshot {
  return useSyncExternalStore(subscribeLedger, getLedgerSnapshot, getServerLedgerSnapshot);
}

export function useDrainerStatus(): DrainerStatus {
  return useSyncExternalStore(subscribeDrainerStatus, getDrainerStatus, getServerDrainerStatus);
}

/**
 * Folder counts with every action the server may not have counted yet --
 * the list itself when nothing changes, so a memo keyed on it holds.
 *
 * @param items anything carrying counts
 * @param folderIdsOf the folders an item's counts sum (one, or a unified
 *   view's members)
 * @param dataUpdatedAt when the counts were read
 */
export function useProjectedCounts<T extends { unread_count: number; total_count: number }>(
  items: T[] | undefined,
  folderIdsOf: (item: T) => readonly string[],
  dataUpdatedAt: number,
): T[] | undefined {
  const { intents } = useIntentLedger();
  return useMemo(() => {
    if (!items || intents.length === 0) return items;
    const deltas = folderCountDeltas(intents, dataUpdatedAt);
    if (deltas.size === 0) return items;
    let changed = false;
    const out = items.map((item) => {
      const next = projectCounts(item, folderIdsOf(item), deltas);
      if (next !== item) changed = true;
      return next;
    });
    return changed ? out : items;
    // folderIdsOf is a pure accessor per call site.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, intents, dataUpdatedAt]);
}
