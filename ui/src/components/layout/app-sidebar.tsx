"use client";

import { useEffect, useMemo } from "react";
import { useAtom, useSetAtom } from "jotai";
import {
  Inbox,
  Send,
  Trash2,
  Archive,
  AlertTriangle,
  CalendarDays,
  Contact,
  FileEdit,
  Folder,
  Layers,
  Mail,
  Settings,
  Search,
  ShieldAlert,
  UserCircle,
  ChevronDown,
  RefreshCw,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Truncate } from "@/components/ui/truncate";
import { folderDisplayName } from "@/lib/folders";
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
import { FolderManageDialog } from "@/components/sidebar/folder-manage-dialog";
import { FolderRowMenu } from "@/components/sidebar/folder-row-menu";
import { CalendarSidebar } from "@/components/calendar/calendar-sidebar";
import { ClientOnly } from "@/components/client-only";
import { ComposeDialog } from "@/components/mail/compose-dialog";
import { NotificationBell } from "@/components/layout/notification-bell";
import { useAccounts } from "@/hooks/use-accounts";
import { useFolders } from "@/hooks/use-folders";
import { useFolderOrder } from "@/hooks/use-folder-order";
import { useUnifiedFolders } from "@/hooks/use-unified-view";
import { useMarkAccountView } from "@/hooks/use-open-message";
import {
  isUnifiedViewAtom,
  requestSelectMailAtom,
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedUnifiedFolderAtom,
} from "@/lib/atoms";
import type {
  AccountResponse,
  FolderResponse,
  FolderOrderItem,
  UnifiedFolderResponse,
} from "@/types/api";

const SPECIAL_USE_ICONS: Record<string, typeof Inbox> = {
  inbox: Inbox,
  sent: Send,
  trash: Trash2,
  archive: Archive,
  junk: AlertTriangle,
  drafts: FileEdit,
};

const SPECIAL_USE_ORDER = [
  "inbox",
  "drafts",
  "sent",
  "archive",
  "junk",
  "trash",
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

/** Which accounts feed a merged unified folder, deduplicated -- a folder
 * called "Inbox" the same way on three accounts merges into one row, and
 * this is the only place that row still says which accounts those were.
 * The tooltip already carried a bare count; this is what makes it visible
 * without hovering, in the same emoji-badge language the unified mail
 * list uses for the same purpose (unified-mail-item.tsx). */
function getContributingAccounts(
  uf: UnifiedFolderResponse,
  accounts: AccountResponse[] | undefined,
): AccountResponse[] {
  if (!accounts) return [];
  const seen = new Set<string>();
  const result: AccountResponse[] = [];
  for (const f of uf.folders) {
    if (seen.has(f.account_id)) continue;
    seen.add(f.account_id);
    const account = accounts.find((a) => a.id === f.account_id);
    if (account) result.push(account);
  }
  return result;
}

/**
 * Drafts never carry an unread state, so the unread-count badge that every
 * other folder uses is always zero there -- show the total instead, which
 * is what tells you a draft is sitting unsent.
 */
function getFolderBadgeCount(folder: FolderResponse | FolderOrderItem): number {
  return folder.special_use === "drafts" ? folder.total_count : folder.unread_count;
}

export function AppSidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const isCalendarRoute = pathname.startsWith("/calendar");
  const isMailRoute = pathname === "/";
  const [selectedAccountId, setSelectedAccountId] = useAtom(
    selectedAccountIdAtom,
  );
  const [selectedFolderId, setSelectedFolderId] = useAtom(
    selectedFolderIdAtom,
  );
  const requestSelectMail = useSetAtom(requestSelectMailAtom);
  const [selectedUnifiedFolder, setSelectedUnifiedFolder] = useAtom(
    selectedUnifiedFolderAtom,
  );
  const isUnified = useAtom(isUnifiedViewAtom)[0];
  const { data: accounts } = useAccounts();
  const { data: folders, isPlaceholderData: foldersArePlaceholder } = useFolders(
    isUnified ? null : selectedAccountId,
  );
  const { data: folderOrderData } = useFolderOrder(
    isUnified ? null : selectedAccountId,
  );
  const { data: unifiedFolders } = useUnifiedFolders();
  const markAccountView = useMarkAccountView();

  const currentAccount = isUnified
    ? null
    : accounts?.find((a) => a.id === selectedAccountId) ?? null;

  // Auto-select first account if none selected
  useEffect(() => {
    if (!isUnified && !selectedAccountId && accounts?.length) {
      setSelectedAccountId(accounts[0].id);
    }
  }, [isUnified, selectedAccountId, accounts, setSelectedAccountId]);

  // Use custom folder order if available, with visibility filtering. INBOX
  // always leads regardless of the saved order -- the API's default order
  // (before anyone has dragged a folder) is alphabetical, which buries it.
  const orderedFolders = useMemo<FolderOrderItem[] | null>(() => {
    if (!folderOrderData?.folders) return null;
    const visible = folderOrderData.folders.filter((f) => f.is_visible);
    const inboxIndex = visible.findIndex((f) => f.special_use === "inbox");
    if (inboxIndex <= 0) return visible;
    const [inbox] = visible.splice(inboxIndex, 1);
    return [inbox, ...visible];
  }, [folderOrderData]);

  // Fallback to legacy sorted folders (visible ones only)
  const sortedFolders = useMemo(
    () => (folders ? sortFolders(folders.filter((f) => f.is_visible)) : []),
    [folders],
  );

  // Auto-select inbox folder when account changes or folders load.
  //
  // `useFolders` keeps the previous account's folders as placeholder data
  // while the new account's request is in flight, so the fallback branch
  // below must not act on it -- it belongs to the account that was
  // selected a moment ago, not the one selected now. `useFolderOrder` has
  // no such placeholder: it returns undefined for an account it hasn't
  // fetched yet, so `orderedFolders` is never stale for the wrong account.
  useEffect(() => {
    if (isUnified || selectedFolderId) return;

    if (orderedFolders && orderedFolders.length > 0) {
      const inbox = orderedFolders.find((f) => f.special_use === "inbox");
      setSelectedFolderId(inbox ? inbox.folder_id : orderedFolders[0].folder_id);
    } else if (sortedFolders.length > 0 && !foldersArePlaceholder) {
      const inbox = sortedFolders.find((f) => f.special_use === "inbox");
      setSelectedFolderId(inbox ? inbox.id : sortedFolders[0].id);
    }
  }, [
    isUnified,
    selectedFolderId,
    orderedFolders,
    sortedFolders,
    foldersArePlaceholder,
    setSelectedFolderId,
  ]);

  // Auto-select the first unified folder when switching into the unified
  // view -- without this, the list area reads "No messages in this
  // folder" (the same empty state a genuinely empty folder shows) rather
  // than the honest "nothing chosen yet" it actually is, until the reader
  // clicks one by hand.
  useEffect(() => {
    if (!isUnified || selectedUnifiedFolder) return;
    if (unifiedFolders && unifiedFolders.length > 0) {
      setSelectedUnifiedFolder(unifiedFolders[0].unified_name);
    }
  }, [isUnified, selectedUnifiedFolder, unifiedFolders, setSelectedUnifiedFolder]);

  /** Select a folder and navigate to the mail view if on a different page. */
  const handleFolderSelect = (folderId: string) => {
    markAccountView();
    setSelectedFolderId(folderId);
    requestSelectMail(null);
    if (pathname !== "/") {
      router.push("/");
    }
  };

  /** Select a unified folder and navigate to the mail view if on a different page. */
  const handleUnifiedFolderSelect = (folderName: string) => {
    setSelectedUnifiedFolder(folderName);
    setSelectedFolderId(null);
    requestSelectMail(null);
    if (pathname !== "/") {
      router.push("/");
    }
  };

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem className="flex items-center gap-1">
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
                  <Truncate
                    text={isUnified ? "Unified View" : currentAccount?.name ?? "Select Account"}
                  />
                </div>
                <ChevronDown className="h-4 w-4 opacity-50" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-56">
                <DropdownMenuItem
                  onClick={() => {
                    setSelectedAccountId("unified");
                    setSelectedFolderId(null);
                    requestSelectMail(null);
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
                      markAccountView();
                      setSelectedAccountId(account.id);
                      setSelectedFolderId(null);
                      requestSelectMail(null);
                      setSelectedUnifiedFolder(null);
                    }}
                  >
                    {account.emoji ? (
                      <span className="mr-2 text-sm">{account.emoji}</span>
                    ) : (
                      <UserCircle className="mr-2 h-4 w-4" />
                    )}
                    <Truncate text={account.name} />
                    {account.id === selectedAccountId && !isUnified && (
                      <span className="ml-auto text-xs text-muted-foreground">
                        current
                      </span>
                    )}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
            <div className="flex items-center gap-1 group-data-[collapsible=icon]:hidden">
              <NotificationBell />
            </div>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <ComposeDialog />
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        {isCalendarRoute && (
          <ClientOnly>
            {/* A mini-month grid and a per-calendar checkbox list have no
             * icon-sized form -- squeezed into the collapsed rail's width
             * they overlap and lose their labels rather than degrading
             * gracefully the way a folder's icon+tooltip does. Hiding the
             * group entirely below the icon-mode breakpoint leaves the
             * rail readable; expanding the rail is what gets them back,
             * same as it is for switching folders on the mail route. */}
            <div className="group-data-[collapsible=icon]:hidden">
              <CalendarSidebar />
            </div>
          </ClientOnly>
        )}
        {isMailRoute && (
        <SidebarGroup>
          <SidebarGroupLabel className="flex items-center justify-between gap-2 pr-1">
            <span>{isUnified ? "Unified Folders" : "Folders"}</span>
            {!isUnified && selectedAccountId && (
              <FolderManageDialog accountId={selectedAccountId} />
            )}
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {isUnified
                ? /* Unified view: merged folders */
                  (unifiedFolders ?? []).map((uf) => {
                    const isActive =
                      selectedUnifiedFolder === uf.unified_name;
                    const folderMapping = uf.folders.map((f) => ({
                      account_id: f.account_id,
                      folder_id: f.folder_id,
                    }));
                    const contributing = getContributingAccounts(uf, accounts);
                    return (
                      <DroppableFolder
                        key={uf.unified_name}
                        folderId={folderMapping[0]?.folder_id ?? uf.unified_name}
                        folderName={uf.unified_name}
                        folderMapping={folderMapping}
                      >
                        <SidebarMenuItem>
                          <SidebarMenuButton
                            isActive={isActive}
                            onClick={() => handleUnifiedFolderSelect(uf.unified_name)}
                            tooltip={`${uf.unified_name} (${uf.folders.length} folder${uf.folders.length === 1 ? "" : "s"})`}
                          >
                            {uf.emoji ? (
                              <span
                                data-testid="unified-view-emoji"
                                aria-hidden
                                className="flex h-4 w-4 shrink-0 items-center justify-center text-sm leading-none"
                              >
                                {uf.emoji}
                              </span>
                            ) : (
                              <Layers className="h-4 w-4" />
                            )}
                            <Truncate text={uf.unified_name} className="flex-1" />
                            {/* Which accounts merged into this row -- the
                                tooltip above only ever gave a count, not
                                visible without hovering. */}
                            {contributing.length > 1 && (
                              <span className="flex shrink-0 items-center -space-x-1 group-data-[collapsible=icon]:hidden">
                                {contributing.map((a) => (
                                  <span
                                    key={a.id}
                                    title={a.name}
                                    className="text-[11px] leading-none"
                                  >
                                    {a.emoji ?? "✉️"}
                                  </span>
                                ))}
                              </span>
                            )}
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
                      </DroppableFolder>
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
                          folderName={folderDisplayName(folder)}
                        >
                          <SidebarMenuItem className="flex items-center gap-1">
                            <SidebarMenuButton
                              isActive={isActive}
                              onClick={() => handleFolderSelect(folder.folder_id)}
                              tooltip={folder.imap_name}
                            >
                              <Icon className="h-4 w-4" />
                              <Truncate text={folderDisplayName(folder)} className="flex-1" />
                            </SidebarMenuButton>
                            {selectedAccountId && (
                              <FolderRowMenu
                                accountId={selectedAccountId}
                                folderId={folder.folder_id}
                                folderName={folderDisplayName(folder)}
                                badgeCount={getFolderBadgeCount(folder)}
                                totalCount={folder.total_count}
                              />
                            )}
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
                          folderName={folderDisplayName(folder)}
                        >
                          <SidebarMenuItem className="flex items-center gap-1">
                            <SidebarMenuButton
                              isActive={isActive}
                              onClick={() => handleFolderSelect(folder.id)}
                              tooltip={folder.imap_name}
                            >
                              <Icon className="h-4 w-4" />
                              <Truncate text={folderDisplayName(folder)} className="flex-1" />
                            </SidebarMenuButton>
                            {selectedAccountId && (
                              <FolderRowMenu
                                accountId={selectedAccountId}
                                folderId={folder.id}
                                folderName={folderDisplayName(folder)}
                                badgeCount={getFolderBadgeCount(folder)}
                                totalCount={folder.total_count}
                              />
                            )}
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
                  No unified views yet.{" "}
                  <Link href="/settings" className="underline">
                    Create one in Settings
                  </Link>{" "}
                  and choose which folders, from any account, it shows.
                </div>
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        )}
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
              render={<Link href="/" />}
              isActive={pathname === "/"}
            >
              <Mail className="h-4 w-4" />
              <span>Mail</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              render={<Link href="/calendar" />}
              isActive={pathname.startsWith("/calendar")}
            >
              <CalendarDays className="h-4 w-4" />
              <span>Calendar</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              render={<Link href="/contacts" />}
              isActive={pathname.startsWith("/contacts")}
            >
              <Contact className="h-4 w-4" />
              <span>Contacts</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              render={<Link href="/spam-review" />}
              isActive={pathname === "/spam-review"}
            >
              <ShieldAlert className="h-4 w-4" />
              <span>Spam review</span>
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
              render={<Link href="/pipeline" />}
              isActive={pathname === "/pipeline"}
            >
              <Workflow className="h-4 w-4" />
              <span>Pipeline</span>
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
