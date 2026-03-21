"use client";

import { useAtom } from "jotai";
import {
  Inbox,
  Send,
  Trash2,
  Archive,
  AlertTriangle,
  FileEdit,
  Folder,
  Layers,
  Mail,
  Settings,
  Search,
  UserCircle,
  ChevronDown,
  RefreshCw,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";

import { DroppableFolder } from "@/components/sidebar/droppable-folder";
import { useAccounts } from "@/hooks/use-accounts";
import { useFolders } from "@/hooks/use-folders";
import { useFolderOrder } from "@/hooks/use-folder-order";
import { useUnifiedFolders } from "@/hooks/use-unified-view";
import {
  isUnifiedViewAtom,
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedMailIdAtom,
  selectedUnifiedFolderAtom,
} from "@/lib/atoms";
import type { FolderResponse, FolderOrderItem, UnifiedFolderResponse } from "@/types/api";

const SPECIAL_USE_ICONS: Record<string, typeof Inbox> = {
  inbox: Inbox,
  sent: Send,
  trash: Trash2,
  archive: Archive,
  junk: AlertTriangle,
  drafts: FileEdit,
  // Legacy backslash format
  "\\Inbox": Inbox,
  "\\Sent": Send,
  "\\Trash": Trash2,
  "\\Archive": Archive,
  "\\Junk": AlertTriangle,
  "\\Drafts": FileEdit,
};

const SPECIAL_USE_ORDER = [
  "\\Inbox", "inbox",
  "\\Drafts", "drafts",
  "\\Sent", "sent",
  "\\Archive", "archive",
  "\\Junk", "junk",
  "\\Trash", "trash",
];

function sortFolders(folders: FolderResponse[]): FolderResponse[] {
  const special = folders.filter((f) => f.special_use);
  const regular = folders.filter((f) => !f.special_use);

  special.sort((a, b) => {
    const ai = SPECIAL_USE_ORDER.indexOf(a.special_use!);
    const bi = SPECIAL_USE_ORDER.indexOf(b.special_use!);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  regular.sort((a, b) => a.imap_name.localeCompare(b.imap_name));

  return [...special, ...regular];
}

function getFolderIcon(folder: FolderResponse | FolderOrderItem) {
  const specialUse = "special_use" in folder ? folder.special_use : null;
  if (specialUse && SPECIAL_USE_ICONS[specialUse]) {
    return SPECIAL_USE_ICONS[specialUse];
  }
  return Folder;
}

function getFolderDisplayName(folder: FolderResponse): string {
  if (folder.display_name) return folder.display_name;
  return folder.imap_name;
}

export function AppSidebar() {
  const pathname = usePathname();
  const [selectedAccountId, setSelectedAccountId] = useAtom(
    selectedAccountIdAtom,
  );
  const [selectedFolderId, setSelectedFolderId] = useAtom(
    selectedFolderIdAtom,
  );
  const [, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const [selectedUnifiedFolder, setSelectedUnifiedFolder] = useAtom(
    selectedUnifiedFolderAtom,
  );
  const isUnified = useAtom(isUnifiedViewAtom)[0];
  const { data: accounts } = useAccounts();
  const { data: folders } = useFolders(
    isUnified ? null : selectedAccountId,
  );
  const { data: folderOrderData } = useFolderOrder(
    isUnified ? null : selectedAccountId,
  );
  const { data: unifiedFolders } = useUnifiedFolders();

  // Auto-select first account if none selected
  const currentAccount = isUnified
    ? null
    : accounts?.find((a) => a.id === selectedAccountId) ?? accounts?.[0];
  if (currentAccount && !selectedAccountId) {
    setSelectedAccountId(currentAccount.id);
  }

  // Use custom folder order if available, with visibility filtering
  const orderedFolders: FolderOrderItem[] | null = folderOrderData?.folders
    ? folderOrderData.folders.filter((f) => f.is_visible)
    : null;

  // Fallback to legacy sorted folders (visible ones only)
  const sortedFolders = folders
    ? sortFolders(folders.filter((f) => f.is_visible))
    : [];

  // Auto-select inbox folder if none selected
  if (!isUnified && !selectedFolderId) {
    if (orderedFolders && orderedFolders.length > 0) {
      const inbox = orderedFolders.find(
        (f) => f.special_use === "inbox" || f.special_use === "\\Inbox",
      );
      setSelectedFolderId(inbox ? inbox.folder_id : orderedFolders[0].folder_id);
    } else if (sortedFolders.length > 0) {
      const inbox = sortedFolders.find(
        (f) => f.special_use === "inbox" || f.special_use === "\\Inbox",
      );
      setSelectedFolderId(inbox ? inbox.id : sortedFolders[0].id);
    }
  }

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <SidebarMenuButton className="w-full justify-between" />
                }
              >
                <div className="flex items-center gap-2">
                  {isUnified ? (
                    <Layers className="h-4 w-4" />
                  ) : currentAccount?.emoji ? (
                    <span className="text-sm">{currentAccount.emoji}</span>
                  ) : (
                    <Mail className="h-4 w-4" />
                  )}
                  <span className="truncate">
                    {isUnified
                      ? "Unified View"
                      : currentAccount?.name ?? "Select Account"}
                  </span>
                </div>
                <ChevronDown className="h-4 w-4 opacity-50" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-56">
                <DropdownMenuItem
                  onClick={() => {
                    setSelectedAccountId("unified");
                    setSelectedFolderId(null);
                    setSelectedMailId(null);
                    setSelectedUnifiedFolder(null);
                  }}
                >
                  <Layers className="mr-2 h-4 w-4" />
                  <span>Unified View</span>
                  {isUnified && (
                    <span className="ml-auto text-xs text-muted-foreground">
                      current
                    </span>
                  )}
                </DropdownMenuItem>
                {accounts?.map((account) => (
                  <DropdownMenuItem
                    key={account.id}
                    onClick={() => {
                      setSelectedAccountId(account.id);
                      setSelectedFolderId(null);
                      setSelectedMailId(null);
                      setSelectedUnifiedFolder(null);
                    }}
                  >
                    {account.emoji ? (
                      <span className="mr-2 text-sm">{account.emoji}</span>
                    ) : (
                      <UserCircle className="mr-2 h-4 w-4" />
                    )}
                    <span className="truncate">{account.name}</span>
                    {account.id === selectedAccountId && !isUnified && (
                      <span className="ml-auto text-xs text-muted-foreground">
                        current
                      </span>
                    )}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>
            {isUnified ? "Unified Folders" : "Folders"}
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {isUnified
                ? /* Unified view: merged folders */
                  (unifiedFolders ?? []).map((uf) => {
                    const isActive =
                      selectedUnifiedFolder === uf.unified_name;
                    return (
                      <SidebarMenuItem key={uf.unified_name}>
                        <SidebarMenuButton
                          isActive={isActive}
                          onClick={() => {
                            setSelectedUnifiedFolder(uf.unified_name);
                            setSelectedFolderId(null);
                            setSelectedMailId(null);
                          }}
                          tooltip={`${uf.unified_name} (${uf.folders.length} accounts)`}
                        >
                          <Layers className="h-4 w-4" />
                          <span className="flex-1 truncate">
                            {uf.unified_name}
                          </span>
                          {uf.unread_count > 0 && (
                            <Badge
                              variant="secondary"
                              className="ml-auto h-5 min-w-5 justify-center px-1 text-xs"
                            >
                              {uf.unread_count}
                            </Badge>
                          )}
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    );
                  })
                : /* Single-account view */
                  orderedFolders
                  ? orderedFolders.map((folder) => {
                      const Icon = getFolderIcon(folder);
                      const isActive = folder.folder_id === selectedFolderId;
                      return (
                        <DroppableFolder
                          key={folder.folder_id}
                          folderId={folder.folder_id}
                        >
                          <SidebarMenuItem>
                            <SidebarMenuButton
                              isActive={isActive}
                              onClick={() => {
                                setSelectedFolderId(folder.folder_id);
                                setSelectedMailId(null);
                              }}
                              tooltip={folder.imap_name}
                            >
                              <Icon className="h-4 w-4" />
                              <span className="flex-1 truncate">
                                {folder.imap_name}
                              </span>
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
                        </DroppableFolder>
                      );
                    })
                  : sortedFolders.map((folder) => {
                      const Icon = getFolderIcon(folder);
                      const isActive = folder.id === selectedFolderId;
                      return (
                        <DroppableFolder
                          key={folder.id}
                          folderId={folder.id}
                        >
                          <SidebarMenuItem>
                            <SidebarMenuButton
                              isActive={isActive}
                              onClick={() => {
                                setSelectedFolderId(folder.id);
                                setSelectedMailId(null);
                              }}
                              tooltip={getFolderDisplayName(folder)}
                            >
                              <Icon className="h-4 w-4" />
                              <span className="flex-1 truncate">
                                {getFolderDisplayName(folder)}
                              </span>
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
                        </DroppableFolder>
                      );
                    })}
              {!isUnified && !orderedFolders && sortedFolders.length === 0 && !selectedAccountId && (
                <div className="px-4 py-3 text-sm text-muted-foreground">
                  Select an account to view folders
                </div>
              )}
              {!isUnified && !orderedFolders && sortedFolders.length === 0 && selectedAccountId && (
                <div className="flex items-center gap-2 px-4 py-3 text-sm text-muted-foreground">
                  <RefreshCw className="h-3 w-3 animate-spin" />
                  Loading folders...
                </div>
              )}
              {isUnified && (!unifiedFolders || unifiedFolders.length === 0) && (
                <div className="px-4 py-3 text-sm text-muted-foreground">
                  No unified folders configured
                </div>
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              render={<Link href="/search" />}
              isActive={pathname === "/search"}
            >
              <Search className="h-4 w-4" />
              <span>Search</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              render={<Link href="/accounts" />}
              isActive={pathname === "/accounts"}
            >
              <UserCircle className="h-4 w-4" />
              <span>Accounts</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              render={<Link href="/settings" />}
              isActive={pathname === "/settings"}
            >
              <Settings className="h-4 w-4" />
              <span>Settings</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}
