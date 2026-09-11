"use client";

/**
 * The phone list's own top bar: which account and folder is open, since
 * nothing else on screen says so, and a tap opens the same sidebar sheet
 * the hamburger button does -- a second, more discoverable route to it
 * that also names where you already are.
 */

import { useAtomValue } from "jotai";
import { ChevronDown } from "lucide-react";
import { useSidebar } from "@/components/ui/sidebar";
import { useAccount } from "@/hooks/use-accounts";
import { useFolders } from "@/hooks/use-folders";
import { useUnifiedFolders } from "@/hooks/use-unified-view";
import { folderDisplayName } from "@/lib/folders";
import {
  isUnifiedViewAtom,
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedUnifiedFolderAtom,
} from "@/lib/atoms";

export function MobileMailHeader() {
  const { toggleSidebar } = useSidebar();
  const isUnifiedView = useAtomValue(isUnifiedViewAtom);
  const accountId = useAtomValue(selectedAccountIdAtom);
  const folderId = useAtomValue(selectedFolderIdAtom);
  const unifiedViewName = useAtomValue(selectedUnifiedFolderAtom);

  const { data: account } = useAccount(isUnifiedView ? null : accountId);
  const { data: folders } = useFolders(isUnifiedView ? null : accountId);
  const { data: unifiedFolders } = useUnifiedFolders();

  const folder = folders?.find((f) => f.id === folderId);
  const unifiedView = unifiedFolders?.find((v) => v.unified_name === unifiedViewName);

  const emoji = isUnifiedView ? unifiedView?.emoji : account?.emoji;
  const name = isUnifiedView ? (unifiedViewName ?? "Mail") : folder ? folderDisplayName(folder) : "Mail";

  return (
    <button
      type="button"
      onClick={toggleSidebar}
      className="flex w-full items-center gap-1.5 border-b px-3 py-2 text-left active:bg-accent/50"
      aria-label={`${name} -- open accounts and folders`}
    >
      {emoji && <span className="text-base leading-none">{emoji}</span>}
      <span className="min-w-0 flex-1 truncate text-sm font-medium">{name}</span>
      <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    </button>
  );
}
