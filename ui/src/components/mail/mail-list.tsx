"use client";

import { useCallback, useRef } from "react";
import { VList, type VListHandle } from "virtua";
import { useAtom, useAtomValue } from "jotai";
import { Loader2, Inbox as InboxIcon } from "lucide-react";

import { MailListItem } from "@/components/mail/mail-list-item";
import { UnifiedMailItem } from "@/components/mail/unified-mail-item";
import { DragMail } from "@/components/mail/drag-mail";
import { BulkToolbar } from "@/components/mail/bulk-toolbar";
import { Skeleton } from "@/components/ui/skeleton";
import { useMailList, useMailAction } from "@/hooks/use-mails";
import { useUnifiedMails } from "@/hooks/use-unified-view";
import {
  useSelection,
  useToggleSelection,
  useRangeSelection,
} from "@/hooks/use-selection";
import { useKeyboardShortcuts } from "@/hooks/use-keyboard-shortcuts";
import {
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedMailIdAtom,
  isUnifiedViewAtom,
  selectedUnifiedFolderAtom,
} from "@/lib/atoms";
import {
  lastClickedMailIdAtom,
  selectionModeAtom,
} from "@/store/selection-atom";
import { focusedMailIndexAtom } from "@/store/focused-mail-atom";
import type { MailSummary, UnifiedMailSummary } from "@/types/api";

export function MailList() {
  const accountId = useAtomValue(selectedAccountIdAtom);
  const folderId = useAtomValue(selectedFolderIdAtom);
  const isUnifiedView = useAtomValue(isUnifiedViewAtom);
  const selectedUnifiedFolder = useAtomValue(selectedUnifiedFolderAtom);
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const [lastClickedId, setLastClickedId] = useAtom(lastClickedMailIdAtom);
  const focusedIndex = useAtomValue(focusedMailIndexAtom);
  const selectionMode = useAtomValue(selectionModeAtom);
  const { selectedIds: checkedIds } = useSelection();
  const toggleSelection = useToggleSelection();
  const rangeSelection = useRangeSelection();
  const mailAction = useMailAction();
  const vlistRef = useRef<VListHandle>(null);

  // Use unified view hook if in unified mode, otherwise use single-account hook
  const unifiedResult = useUnifiedMails(
    isUnifiedView ? selectedUnifiedFolder : null,
  );
  const singleAccountResult = useMailList(
    isUnifiedView ? null : accountId,
    folderId,
  );

  const result = isUnifiedView ? unifiedResult : singleAccountResult;
  const {
    data,
    isLoading,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
  } = result;

  const allMails: (MailSummary | UnifiedMailSummary)[] =
    data?.pages.flatMap((p) => p.mails) ?? [];

  const scrollToIndex = useCallback(
    (index: number) => {
      vlistRef.current?.scrollToIndex(index, { align: "nearest" });
    },
    [],
  );

  useKeyboardShortcuts({ mails: allMails, scrollToIndex });

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
    (
      mailId: string,
      action: "flag" | "unflag" | "archive" | "spam" | "delete" | "mark_read" | "mark_unread",
      mailAccountId?: string,
    ) => {
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

  const handleCheckToggle = useCallback(
    (mailId: string, shiftKey: boolean) => {
      if (!accountId) return;

      if (shiftKey && lastClickedId && folderId) {
        // Shift-click: range select
        rangeSelection.mutate({
          accountId,
          body: {
            from_id: lastClickedId,
            to_id: mailId,
            folder_id: folderId,
          },
        });
      } else {
        // Regular click: toggle
        toggleSelection.mutate({
          accountId,
          body: { mail_id: mailId },
        });
      }
      setLastClickedId(mailId);
    },
    [
      accountId,
      folderId,
      lastClickedId,
      rangeSelection,
      toggleSelection,
      setLastClickedId,
    ],
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

  if (allMails.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <InboxIcon className="h-12 w-12 opacity-50" />
        <p className="text-sm">No messages in this folder</p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <BulkToolbar />
      <VList
        ref={vlistRef}
        className="flex-1"
        style={{ height: "100%" }}
        itemSize={76}
        onScroll={handleScroll}
      >
        {allMails.map((mail, index) =>
          isUnifiedView ? (
            <DragMail key={mail.id} mailId={mail.id}>
              <UnifiedMailItem
                mail={mail as UnifiedMailSummary}
                isSelected={mail.id === selectedMailId}
                isFocused={index === focusedIndex}
                isChecked={checkedIds.has(mail.id)}
                selectionMode={selectionMode}
                onSelect={setSelectedMailId}
                onCheckToggle={handleCheckToggle}
                onAction={handleAction}
              />
            </DragMail>
          ) : (
            <DragMail key={mail.id} mailId={mail.id}>
              <MailListItem
                mail={mail as MailSummary}
                isSelected={mail.id === selectedMailId}
                isFocused={index === focusedIndex}
                isChecked={checkedIds.has(mail.id)}
                selectionMode={selectionMode}
                onSelect={setSelectedMailId}
                onCheckToggle={handleCheckToggle}
                onAction={handleAction}
              />
            </DragMail>
          ),
        )}
      </VList>
      {isFetchingNextPage && (
        <div className="flex items-center justify-center py-3">
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        </div>
      )}
    </div>
  );
}
