"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { VList, type VListHandle } from "virtua";
import { useAtom, useAtomValue } from "jotai";
import { Loader2, Inbox as InboxIcon, Layers } from "lucide-react";

import { MailListItem } from "@/components/mail/mail-list-item";
import { UnifiedMailItem } from "@/components/mail/unified-mail-item";
import { DragMail } from "@/components/mail/drag-mail";
import { SelectionBanner } from "@/components/mail/selection-banner";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { useMailList, useMailAction } from "@/hooks/use-mails";
import { useFolders } from "@/hooks/use-folders";
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

  // Mail arriving above a scrolled-away reader must not move a single row
  // on screen. `shift` is virtua's own mechanism for exactly this -- it
  // realigns its measured-height cache to the new indices and compensates
  // scrollOffset by the real delta once the new rows are measured, rather
  // than a guessed pixel count. It only applies for the render where the
  // prepend actually lands: an append (older mail paged in at the tail)
  // must never see it, or the cache misaligns the other way.
  const prevDataRef = useRef(data);
  const prevMailIdsRef = useRef<string[]>([]);
  const [shiftForPrepend, setShiftForPrepend] = useState(false);
  if (data !== prevDataRef.current) {
    const isPrepend = countPrepended(prevMailIdsRef.current, allMailIds) > 0;
    prevDataRef.current = data;
    prevMailIdsRef.current = allMailIds;
    if (isPrepend !== shiftForPrepend) setShiftForPrepend(isPrepend);
  }

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
        loadedIds={allMailIds}
      />
      {allMails.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
          <InboxIcon className="h-12 w-12 opacity-50" />
          <p className="text-sm">No messages in this folder</p>
        </div>
      ) : (
        <VList
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
