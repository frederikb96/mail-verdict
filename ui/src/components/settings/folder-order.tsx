"use client";

import { useState, useEffect } from "react";
import {
  Save,
  Loader2,
  ChevronUp,
  ChevronDown,
  Eye,
  EyeOff,
  Inbox,
  Send,
  Trash2,
  Archive,
  AlertTriangle,
  FileEdit,
  Folder,
  GripVertical,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  useFolderOrder,
  useUpdateFolderOrder,
  useToggleFolderVisibility,
} from "@/hooks/use-folder-order";
import type { FolderOrderItem } from "@/types/api";

const SPECIAL_USE_ICONS: Record<string, typeof Inbox> = {
  inbox: Inbox,
  sent: Send,
  trash: Trash2,
  archive: Archive,
  junk: AlertTriangle,
  drafts: FileEdit,
};

function getFolderIcon(specialUse: string | null) {
  if (specialUse && SPECIAL_USE_ICONS[specialUse]) {
    return SPECIAL_USE_ICONS[specialUse];
  }
  return Folder;
}

interface FolderOrderProps {
  accountId: string | null;
}

/**
 * Folder ordering and visibility management.
 * Reorder with up/down buttons, toggle visibility per folder.
 */
export function FolderOrder({ accountId }: FolderOrderProps) {
  const { data: orderData } = useFolderOrder(accountId);
  const updateOrder = useUpdateFolderOrder();
  const toggleVisibility = useToggleFolderVisibility();

  const [localFolders, setLocalFolders] = useState<FolderOrderItem[]>([]);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (orderData?.folders) {
      setLocalFolders(orderData.folders);
      setDirty(false);
    }
  }, [orderData]);

  if (!accountId) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <GripVertical className="h-4 w-4" />
            Folder Order
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Select an account to manage folder order
          </p>
        </CardContent>
      </Card>
    );
  }

  const moveFolder = (index: number, direction: -1 | 1) => {
    const newIndex = index + direction;
    if (newIndex < 0 || newIndex >= localFolders.length) return;
    const updated = [...localFolders];
    [updated[index], updated[newIndex]] = [updated[newIndex], updated[index]];
    setLocalFolders(updated);
    setDirty(true);
  };

  const handleSave = () => {
    const order = localFolders.map((f) => f.folder_id);
    updateOrder.mutate(
      { accountId, order },
      { onSuccess: () => setDirty(false) },
    );
  };

  const handleToggleVisibility = (folderId: string, currentVisible: boolean) => {
    toggleVisibility.mutate({
      accountId,
      folderId,
      isVisible: !currentVisible,
    });
    // Optimistic update
    setLocalFolders((prev) =>
      prev.map((f) =>
        f.folder_id === folderId ? { ...f, is_visible: !currentVisible } : f,
      ),
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <GripVertical className="h-4 w-4" />
          Folder Order & Visibility
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-xs text-muted-foreground">
          Reorder folders using the arrow buttons. Hidden folders are excluded
          from the sidebar.
        </p>

        {localFolders.length === 0 && (
          <div className="py-4 text-sm text-muted-foreground">
            No folders available
          </div>
        )}

        {localFolders.length > 0 && (
          <div className="divide-y rounded-md border">
            {localFolders.map((folder, index) => {
              const Icon = getFolderIcon(folder.special_use);
              return (
                <div
                  key={folder.folder_id}
                  className="flex items-center gap-2 px-3 py-1.5"
                >
                  <div className="flex flex-col">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-5 w-5"
                      disabled={index === 0}
                      onClick={() => moveFolder(index, -1)}
                    >
                      <ChevronUp className="h-3 w-3" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-5 w-5"
                      disabled={index === localFolders.length - 1}
                      onClick={() => moveFolder(index, 1)}
                    >
                      <ChevronDown className="h-3 w-3" />
                    </Button>
                  </div>
                  <Icon className="h-4 w-4 text-muted-foreground" />
                  <span
                    className={`flex-1 text-sm ${!folder.is_visible ? "text-muted-foreground line-through" : ""}`}
                  >
                    {folder.imap_name}
                  </span>
                  {folder.unread_count > 0 && (
                    <span className="text-xs text-muted-foreground">
                      {folder.unread_count} unread
                    </span>
                  )}
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() =>
                      handleToggleVisibility(
                        folder.folder_id,
                        folder.is_visible,
                      )
                    }
                    title={folder.is_visible ? "Hide folder" : "Show folder"}
                  >
                    {folder.is_visible ? (
                      <Eye className="h-3.5 w-3.5" />
                    ) : (
                      <EyeOff className="h-3.5 w-3.5 text-muted-foreground" />
                    )}
                  </Button>
                </div>
              );
            })}
          </div>
        )}

        {dirty && (
          <div className="mt-4 flex justify-end">
            <Button
              onClick={handleSave}
              disabled={updateOrder.isPending}
              size="sm"
            >
              {updateOrder.isPending ? (
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
              ) : (
                <Save className="mr-1 h-3 w-3" />
              )}
              Save Order
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
