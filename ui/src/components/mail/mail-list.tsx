"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { VList, type VListHandle } from "virtua";
import { useAtom, useAtomValue } from "jotai";
import { AlertCircle, Loader2, Inbox as InboxIcon, Layers } from "lucide-react";

import { MailListItem } from "@/components/mail/mail-list-item";
import { UnifiedMailItem } from "@/components/mail/unified-mail-item";
import { DragMail } from "@/components/mail/drag-mail";
import { SelectionBanner } from "@/components/mail/selection-banner";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { useMailList, useMailAction } from "@/hooks/use-mails";
import { useFolders } from "@/hooks/use-folders";
import { useAccount } from "@/hooks/use-accounts";
import { accountConnectionState, useSyncStatus } from "@/hooks/use-sync-status";
import { useUnifiedMails } from "@/hooks/use-unified-view";
import { useClearSelection, useSelection, useSelectionGestures } from "@/hooks/use-selection";
import { useKeyboardShortcuts } from "@/hooks/use-keyboard-shortcuts";
import {
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedMailIdAtom,
  isUnifiedViewAtom,
  selectedUnifiedFolderAtom,
  threadedViewAtom,
} from "@/lib/atoms";
import { selectionModeAtom } from "@/store/selection-atom";
import type { SelectableRow } from "@/lib/selection";
import { focusedMailIndexAtom } from "@/store/focused-mail-atom";
import type { MessageActionType, MessageSummary, UnifiedMessageSummary } from "@/types/api";

type RowAction = Extract<
  MessageActionType,
  | "flag"
  | "unflag"
  | "archive"
  | "spam"
  | "not_spam"
  | "trash"
  | "mark_read"
  | "mark_unread"
>;

/** How many ids at the front of `nextIds` are new, given `prevIds` -- zero
 * unless the whole of `prevIds` still appears afterward, in the same order.
 * That is what tells mail arriving above the reader (a real prepend, worth
 * compensating) apart from a folder switch, a threading toggle, or a page
 * appended at the tail: none of those leave the previous list as a
 * contiguous run inside the new one. */
function countPrepended(prevIds: string[], nextIds: string[]): number {
  if (prevIds.length === 0 || nextIds.length <= prevIds.length) return 0;
  const anchor = nextIds.indexOf(prevIds[0]);
  if (anchor <= 0) return 0;
  for (let i = 0; i < prevIds.length; i++) {
    if (nextIds[anchor + i] !== prevIds[i]) return 0;
  }
  return anchor;
}

/** The id still present in both lists nearest to `fromIndex` in `prevIds`,
 * searched outward in both directions -- used when the row that sat at
 * the reader's anchor point is itself the one that moved out from under
 * them, so there is nothing to correct against directly. */
function nearestSurvivor(
  prevIds: string[],
  fromIndex: number,
  nextIdSet: Set<string>,
): { id: string; index: number } | null {
  const clamped = Math.max(0, Math.min(fromIndex, prevIds.length - 1));
  for (let d = 0; d < prevIds.length; d++) {
    const before = clamped - d;
    if (before >= 0 && nextIdSet.has(prevIds[before])) {
      return { id: prevIds[before], index: before };
    }
    const after = clamped + d;
    if (d > 0 && after < prevIds.length && nextIdSet.has(prevIds[after])) {
      return { id: prevIds[after], index: after };
    }
  }
  return null;
}

export function MailList() {
  const accountId = useAtomValue(selectedAccountIdAtom);
  const folderId = useAtomValue(selectedFolderIdAtom);
  const isUnifiedView = useAtomValue(isUnifiedViewAtom);
  const selectedUnifiedFolder = useAtomValue(selectedUnifiedFolderAtom);
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const focusedIndex = useAtomValue(focusedMailIndexAtom);
  const selectionMode = useAtomValue(selectionModeAtom);
  const [threaded, setThreaded] = useAtom(threadedViewAtom);
  const { isSelected } = useSelection();
  const { toggle, shiftRange } = useSelectionGestures();
  const clearSelection = useClearSelection();
  const mailAction = useMailAction();
  const vlistRef = useRef<VListHandle>(null);

  // The same four values decide which list this is, for a selection's own
  // scope below and for `VList`'s own identity further down: a change to
  // any of them is a genuinely different list, not the same one showing
  // different rows.
  const listIdentity = `${accountId}:${folderId}:${isUnifiedView}:${selectedUnifiedFolder}:${threaded}`;

  // A selection is scoped to the list it was made in (see
  // effectiveSelectionAtom): a change to any of these four values is
  // already a scope mismatch, so the guard alone empties it for a folder
  // switch, an account switch and a threading toggle. It does NOT cover
  // leaving the mail view for another one (Calendar, say) and coming back
  // to the very same folder -- none of these four values change across
  // that round trip, so the guard sees no mismatch at all. This effect is
  // what actually clears it for that case: `MailList` unmounts when the
  // route changes and this runs again on the fresh mount, regardless of
  // whether any of its own dependencies moved. It is not a belt-and-braces
  // convenience on top of the guard -- for the view-round-trip shape, it
  // is the only thing that does the clearing.
  useEffect(() => {
    clearSelection();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, folderId, isUnifiedView, selectedUnifiedFolder, threaded]);

  // An account stuck in `error` with no completed sync pass never gets a
  // folder to auto-select, so the loading state below would otherwise
  // spin forever with nothing telling the reader why -- the Accounts page
  // already has this predicate; read it from the same place rather than
  // recomputing "is this account broken" here.
  const { data: currentAccount } = useAccount(isUnifiedView ? null : accountId);
  const { data: currentSyncStatus } = useSyncStatus(isUnifiedView ? null : accountId);
  const neverConnected =
    !!currentAccount &&
    accountConnectionState(currentAccount, currentSyncStatus) === "never_connected";

  // Threading is a single-account concept (thread_id groups per-account folders).
  const unifiedResult = useUnifiedMails(
    isUnifiedView ? selectedUnifiedFolder : null,
  );
  const singleAccountResult = useMailList(
    isUnifiedView ? null : accountId,
    folderId,
    threaded,
  );

  const { data: folders } = useFolders(isUnifiedView ? null : accountId);
  const isJunkFolder = folders?.find((f) => f.id === folderId)?.special_use === "junk";

  const result = isUnifiedView ? unifiedResult : singleAccountResult;
  const {
    data,
    isLoading,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
  } = result;

  const allMails: (MessageSummary | UnifiedMessageSummary)[] =
    data?.pages.flatMap((p) => p.messages) ?? [];
  const allMailIds = allMails.map((m) => m.id);
  const rowsById = useMemo(() => {
    const map = new Map<string, SelectableRow>();
    for (const m of allMails) map.set(m.id, m);
    return map;
  }, [allMails]);

  // Nothing above the reader may move a single row on screen, whatever
  // caused the row count above them to change -- mail arriving at the top,
  // or a message (a classification run, a rule, a drag) leaving the folder
  // from somewhere in the middle. Two mechanisms, gated so exactly one
  // runs per change:
  //
  // - A real prepend hands off to `shift`, virtua's own mechanism for it --
  //   it realigns its measured-height cache to the new indices and
  //   compensates scrollOffset by the real delta once the new rows are
  //   measured, which a pixel read taken here could not do for content
  //   that has never been laid out.
  // - Anything else reads the viewport's own anchor row before the change
  //   commits, then in a layout effect after it corrects scrollOffset by
  //   exactly how far that same row's measured position moved -- the same
  //   snapshot-before/correct-after shape the scrolling skill uses for a
  //   prepend, generalised to a change anywhere above the reader rather
  //   than only at the top.
  //
  // Both assume the reader is still looking at the *same* list, one whose
  // rows merely changed under them -- `nearestSurvivor` below finds
  // nothing and leaves scrollOffset alone otherwise, since a genuinely
  // different list shares no row ids with the old one to search for. A
  // switch to a different list entirely is `VList`'s own `key` further
  // down: without it, virtua carries its previous scrollOffset over to a
  // shorter list's smaller scroll range unchanged, and only clamps it to
  // fit -- landing partway down a list the reader never scrolled, at
  // whatever offset the arithmetic of the two lists' heights happens to
  // produce, rather than at the top.
  const prevDataRef = useRef(data);
  const prevMailIdsRef = useRef<string[]>([]);
  const [shiftForPrepend, setShiftForPrepend] = useState(false);
  const pendingScrollCorrectionRef = useRef<{
    anchorId: string;
    oldOffset: number;
    oldScrollOffset: number;
  } | null>(null);
  if (data !== prevDataRef.current) {
    const prevIds = prevMailIdsRef.current;
    const isPrepend = countPrepended(prevIds, allMailIds) > 0;
    const handle = vlistRef.current;
    if (!isPrepend && handle && prevIds.length > 0) {
      const oldScrollOffset = handle.scrollOffset;
      const oldAnchorIndex = handle.findItemIndex(oldScrollOffset);
      const survivor = nearestSurvivor(prevIds, oldAnchorIndex, new Set(allMailIds));
      if (survivor) {
        pendingScrollCorrectionRef.current = {
          anchorId: survivor.id,
          oldOffset: handle.getItemOffset(survivor.index),
          oldScrollOffset,
        };
      }
    }
    prevDataRef.current = data;
    prevMailIdsRef.current = allMailIds;
    if (isPrepend !== shiftForPrepend) setShiftForPrepend(isPrepend);
  }

  useLayoutEffect(() => {
    const correction = pendingScrollCorrectionRef.current;
    pendingScrollCorrectionRef.current = null;
    const handle = vlistRef.current;
    if (!correction || !handle) return;
    const newIndex = allMailIds.indexOf(correction.anchorId);
    if (newIndex < 0) return;
    const delta = handle.getItemOffset(newIndex) - correction.oldOffset;
    if (delta !== 0) handle.scrollTo(correction.oldScrollOffset + delta);
  }, [allMailIds]);

  const scrollToIndex = useCallback(
    (index: number) => {
      vlistRef.current?.scrollToIndex(index, { align: "nearest" });
    },
    [],
  );

  useKeyboardShortcuts({ mails: allMails as MessageSummary[], scrollToIndex });

  const handleScroll = useCallback(
    (offset: number) => {
      if (!vlistRef.current) return;
      const { scrollSize, viewportSize } = vlistRef.current;
      if (
        scrollSize - offset - viewportSize < 200 &&
        hasNextPage &&
        !isFetchingNextPage
      ) {
        fetchNextPage();
      }
    },
    [hasNextPage, isFetchingNextPage, fetchNextPage],
  );

  const handleAction = useCallback(
    (mailId: string, action: RowAction, mailAccountId?: string) => {
      const account = mailAccountId || accountId;
      if (!account) return;
      mailAction.mutate({
        mailId,
        accountId: account,
        action: { action },
      });
    },
    [accountId, mailAction],
  );

  // A plain click on a row's text abandons any active selection entirely
  // and just opens that message -- checking a checkbox never does this.
  const handleOpen = useCallback(
    (mailId: string) => {
      if (selectionMode) clearSelection();
      setSelectedMailId(mailId);
    },
    [selectionMode, clearSelection, setSelectedMailId],
  );

  const handleCheckToggle = useCallback(
    (mailId: string, shiftKey: boolean) => {
      if (shiftKey) {
        shiftRange(allMailIds, rowsById, mailId);
        return;
      }
      const row = rowsById.get(mailId);
      if (row) toggle(row);
    },
    [allMailIds, rowsById, shiftRange, toggle],
  );

  if (isLoading) {
    return (
      <div className="flex flex-col">
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="flex items-start gap-3 border-b px-4 py-3">
            <Skeleton className="h-8 w-8 shrink-0 rounded-full" />
            <div className="flex flex-1 flex-col gap-1">
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-3 w-48" />
              <Skeleton className="h-3 w-64" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (!accountId) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <InboxIcon className="h-12 w-12 opacity-50" />
        <p className="text-sm">Select an account to view messages</p>
      </div>
    );
  }

  if (!folderId && !isUnifiedView) {
    if (neverConnected) {
      return (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
          <AlertCircle className="h-8 w-8 text-destructive" />
          <p className="text-sm text-destructive">
            {currentAccount?.state_error ?? "This account has never connected"}
          </p>
        </div>
      );
    }
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <Loader2 className="h-8 w-8 animate-spin opacity-50" />
        <p className="text-sm">Loading folders...</p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {!isUnifiedView && (
        <div className="flex items-center justify-between border-b px-3 py-1.5">
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <Switch checked={threaded} onCheckedChange={setThreaded} />
            <Layers className="h-3 w-3" />
            Group by conversation
          </label>
        </div>
      )}
      <SelectionBanner
        accountId={isUnifiedView ? null : accountId}
        folderId={isUnifiedView ? null : folderId}
        threaded={threaded}
        loadedCount={allMailIds.length}
      />
      {allMails.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
          <InboxIcon className="h-12 w-12 opacity-50" />
          <p className="text-sm">No messages in this folder</p>
        </div>
      ) : (
        <VList
          key={listIdentity}
          ref={vlistRef}
          className="flex-1"
          style={{ height: "100%" }}
          itemSize={76}
          shift={shiftForPrepend}
          onScroll={handleScroll}
        >
          {allMails.map((mail, index) =>
            isUnifiedView ? (
              <DragMail key={mail.id} row={mail} accountId={mail.account_id} folderId={mail.folder_id}>
                <UnifiedMailItem
                  mail={mail as UnifiedMessageSummary}
                  isSelected={mail.id === selectedMailId}
                  isFocused={index === focusedIndex}
                  isChecked={isSelected(mail)}
                  selectionMode={selectionMode}
                  onOpen={handleOpen}
                  onCheckToggle={handleCheckToggle}
                  onAction={handleAction}
                />
              </DragMail>
            ) : (
              <DragMail key={mail.id} row={mail} accountId={mail.account_id} folderId={mail.folder_id}>
                <MailListItem
                  mail={mail as MessageSummary}
                  isSelected={mail.id === selectedMailId}
                  isFocused={index === focusedIndex}
                  isChecked={isSelected(mail)}
                  selectionMode={selectionMode}
                  isJunk={isJunkFolder}
                  isThreaded={threaded}
                  onOpen={handleOpen}
                  onCheckToggle={handleCheckToggle}
                  onAction={handleAction}
                />
              </DragMail>
            ),
          )}
        </VList>
      )}
      {isFetchingNextPage && (
        <div className="flex items-center justify-center py-3">
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        </div>
      )}
    </div>
  );
}
