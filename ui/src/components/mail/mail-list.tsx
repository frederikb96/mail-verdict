"use client";

import { useCallback, useRef } from "react";
import { VList, type VListHandle } from "virtua";
import { useAtom, useAtomValue } from "jotai";
import { Loader2, Inbox as InboxIcon } from "lucide-react";

import { MailListItem } from "@/components/mail/mail-list-item";
import { Skeleton } from "@/components/ui/skeleton";
import { useMailList, useMailAction } from "@/hooks/use-mails";
import {
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedMailIdAtom,
} from "@/lib/atoms";
import type { MailSummary } from "@/types/api";

export function MailList() {
  const accountId = useAtomValue(selectedAccountIdAtom);
  const folderId = useAtomValue(selectedFolderIdAtom);
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const mailAction = useMailAction();
  const vlistRef = useRef<VListHandle>(null);

  const {
    data,
    isLoading,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
  } = useMailList(accountId, folderId);

  const allMails: MailSummary[] =
    data?.pages.flatMap((p) => p.mails) ?? [];

  const handleScroll = useCallback(
    (offset: number) => {
      if (!vlistRef.current) return;
      const { scrollSize, viewportSize } = vlistRef.current;
      // Load more when scrolled near the bottom (within 200px)
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
    (mailId: string, action: "flag" | "unflag" | "delete") => {
      if (!accountId) return;
      mailAction.mutate({
        mailId,
        accountId,
        action: { action },
      });
    },
    [accountId, mailAction],
  );

  if (isLoading) {
    return (
      <div className="flex flex-col">
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="flex h-16 items-center gap-3 border-b px-3">
            <Skeleton className="h-8 w-8 rounded-full" />
            <div className="flex flex-1 flex-col gap-1">
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-3 w-48" />
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
      <VList
        ref={vlistRef}
        className="flex-1"
        style={{ height: "100%" }}
        itemSize={64}
        onScroll={handleScroll}
      >
        {allMails.map((mail) => (
          <MailListItem
            key={mail.id}
            mail={mail}
            isSelected={mail.id === selectedMailId}
            onSelect={setSelectedMailId}
            onAction={handleAction}
          />
        ))}
      </VList>
      {isFetchingNextPage && (
        <div className="flex items-center justify-center py-3">
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        </div>
      )}
    </div>
  );
}
