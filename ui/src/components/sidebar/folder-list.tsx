"use client";

import {
  Inbox,
  Send,
  Trash2,
  Archive,
  AlertTriangle,
  FileEdit,
  Folder,
  RefreshCw,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { useFolderOrder } from "@/hooks/use-folder-order";
import type { FolderOrderItem } from "@/types/api";

const SPECIAL_USE_ICONS: Record<string, typeof Inbox> = {
  inbox: Inbox,
  sent: Send,
  trash: Trash2,
  archive: Archive,
  junk: AlertTriangle,
  drafts: FileEdit,
};

function getFolderIcon(item: FolderOrderItem) {
  if (item.special_use && SPECIAL_USE_ICONS[item.special_use]) {
    return SPECIAL_USE_ICONS[item.special_use];
  }
  return Folder;
}

interface FolderListProps {
  accountId: string | null;
  selectedFolderId: string | null;
  onFolderSelect: (folderId: string) => void;
}

/**
 * Sidebar folder list respecting custom order and visibility.
 * Renders all folders flat (no nesting), preserving dot-separated names.
 */
export function FolderList({
  accountId,
  selectedFolderId,
  onFolderSelect,
}: FolderListProps) {
  const { data: orderData, isLoading } = useFolderOrder(accountId);

  const visibleFolders =
    orderData?.folders.filter((f) => f.is_visible) ?? [];

  if (!accountId) {
    return (
      <div className="px-4 py-3 text-sm text-muted-foreground">
        Select an account to view folders
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 px-4 py-3 text-sm text-muted-foreground">
        <RefreshCw className="h-3 w-3 animate-spin" />
        Loading folders...
      </div>
    );
  }

  if (visibleFolders.length === 0) {
    return (
      <div className="px-4 py-3 text-sm text-muted-foreground">
        No visible folders
      </div>
    );
  }

  return (
    <SidebarMenu>
      {visibleFolders.map((folder) => {
        const Icon = getFolderIcon(folder);
        const isActive = folder.folder_id === selectedFolderId;
        return (
          <SidebarMenuItem key={folder.folder_id}>
            <SidebarMenuButton
              isActive={isActive}
              onClick={() => onFolderSelect(folder.folder_id)}
              tooltip={folder.imap_name}
            >
              <Icon className="h-4 w-4" />
              <span className="flex-1 truncate">{folder.imap_name}</span>
              {folder.unread_count > 0 && (
                <Badge
                  variant="secondary"
                  className="ml-auto h-5 min-w-5 justify-center px-1 text-xs"
                >
                  {folder.unread_count}
                </Badge>
              )}
            </SidebarMenuButton>
          </SidebarMenuItem>
        );
      })}
    </SidebarMenu>
  );
}
