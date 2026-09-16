/** React access to the mail intent ledger and the state of its sending. */

import { useMemo, useSyncExternalStore } from "react";
import type { QueryKey } from "@tanstack/react-query";
import {
  getLedgerSnapshot,
  getServerLedgerSnapshot,
  projectableIntents,
  subscribeLedger,
  type LedgerSnapshot,
} from "@/lib/intent-ledger";
import {
  getDrainerStatus,
  getServerDrainerStatus,
  subscribeDrainerStatus,
  type DrainerStatus,
} from "@/lib/intent-drainer";
import { folderCountDeltas, projectCounts, type MailIntent } from "@/lib/mail-intents";
import {
  getObservedChanges,
  getServerObservedChanges,
  subscribeObservedChanges,
} from "@/lib/observed-changes";
import { readTimeOf } from "@/lib/read-clock";

export function useIntentLedger(): LedgerSnapshot {
  return useSyncExternalStore(subscribeLedger, getLedgerSnapshot, getServerLedgerSnapshot);
}

export function useDrainerStatus(): DrainerStatus {
  return useSyncExternalStore(subscribeDrainerStatus, getDrainerStatus, getServerDrainerStatus);
}

/** Everything a screen projects over server data: the ledger's intents not
 * waiting to be undone, and the moves other clients were seen to make. */
export function useProjectionIntents(): readonly MailIntent[] {
  const ledger = useIntentLedger();
  const observed = useSyncExternalStore(
    subscribeObservedChanges, getObservedChanges, getServerObservedChanges,
  );
  return useMemo(
    () => {
      const own = projectableIntents(ledger);
      return observed.length === 0 ? own : [...own, ...observed];
    },
    // The snapshot object changes on every ledger write; its parts do not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ledger.intents, ledger.undoRequests, observed],
  );
}

/**
 * Folder counts with every action the server may not have counted yet --
 * the list itself when nothing changes, so a memo keyed on it holds.
 *
 * @param items anything carrying counts
 * @param folderIdsOf the folders an item's counts sum (one, or a unified
 *   view's members)
 * @param queryKey the counts' query, whose read time decides what they hold
 * @param dataUpdatedAt the query's own dataUpdatedAt
 */
export function useProjectedCounts<T extends { unread_count: number; total_count: number }>(
  items: T[] | undefined,
  folderIdsOf: (item: T) => readonly string[],
  queryKey: QueryKey,
  dataUpdatedAt: number,
): T[] | undefined {
  const ledger = useIntentLedger();
  const readAt = readTimeOf(queryKey, dataUpdatedAt);
  return useMemo(() => {
    const intents = projectableIntents(ledger);
    if (!items || intents.length === 0) return items;
    const deltas = folderCountDeltas(intents, readAt);
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
  }, [items, ledger.intents, ledger.undoRequests, readAt]);
}

/** How many actions not yet answered by the server involve this folder --
 * taking a message out of it or filing one into it. Nothing that destroys
 * a folder's contents may run while any does: a move still in the browser
 * is invisible to the server's own guards. */
export function useUnsettledIntentsForFolder(folderId: string | null): number {
  const { intents } = useIntentLedger();
  return useMemo(() => {
    if (!folderId) return 0;
    return intents.filter(
      (i) =>
        (i.state === "pending" || i.state === "inflight" || i.state === "held") &&
        (i.targetFolderId === folderId || i.messages.some((m) => m.folderId === folderId)),
    ).length;
  }, [intents, folderId]);
}
