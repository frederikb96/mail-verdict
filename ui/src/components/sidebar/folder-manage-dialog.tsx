"use client";

import { useState } from "react";
import { FolderPlus, Loader2, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";

import { useFolders, useCreateFolder, useDeleteFolder } from "@/hooks/use-folders";
import { useToast } from "@/hooks/use-toast";
import type { FolderResponse } from "@/types/api";

const TOP_LEVEL = "__top_level__";

/** Create/delete folders for one account -- a thin form over the folder-management API. */
export function FolderManageDialog({ accountId }: { accountId: string }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  // Left unset rather than defaulting to TOP_LEVEL: the select renders its
  // item labels only once its popup has mounted, so a value chosen before
  // the user ever opens it shows the raw sentinel instead of "Top level".
  const [parentId, setParentId] = useState<string | undefined>(undefined);
  const [pendingDelete, setPendingDelete] = useState<FolderResponse | null>(null);

  const { data: folders } = useFolders(accountId);
  const createFolder = useCreateFolder();
  const deleteFolder = useDeleteFolder();
  const { push: pushToast } = useToast();

  const liveFolders = (folders ?? []).filter((f) => f.special_use !== "inbox");

  const handleCreate = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    createFolder.mutate(
      {
        accountId,
        data: {
          name: trimmed,
          parent_id: parentId && parentId !== TOP_LEVEL ? parentId : undefined,
        },
      },
      {
        onSuccess: () => {
          pushToast(`Folder "${trimmed}" created`, "success");
          setName("");
          setParentId(undefined);
        },
        onError: (err) => pushToast(`Could not create folder: ${err.message}`, "error", 0),
      },
    );
  };

  const handleDelete = (folder: FolderResponse) => {
    deleteFolder.mutate({ folderId: folder.id, messageCount: folder.total_count }, {
      onSuccess: () => {
        pushToast(`Folder "${folder.display_name || folder.imap_name}" deleted`, "success");
        setPendingDelete(null);
      },
      onError: (err) => pushToast(`Could not delete folder: ${err.message}`, "error", 0),
    });
  };

  return (
    <>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger
          render={
            <Button variant="ghost" size="icon" className="h-6 w-6" title="Manage folders" />
          }
        >
          <FolderPlus className="h-3.5 w-3.5" />
        </DialogTrigger>
        <DialogContent size="md">
          <DialogHeader>
            <DialogTitle>Manage folders</DialogTitle>
          </DialogHeader>

          <div className="flex flex-col gap-2">
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="New folder name"
              onKeyDown={(e) => {
                if (e.key === "Enter") handleCreate();
              }}
            />
            <div className="flex items-center gap-2">
              <Select value={parentId} onValueChange={(v) => setParentId(v ?? TOP_LEVEL)}>
                <SelectTrigger className="h-8 flex-1">
                  <SelectValue placeholder="Top level" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={TOP_LEVEL}>Top level</SelectItem>
                  {liveFolders.map((f) => (
                    <SelectItem key={f.id} value={f.id}>
                      {f.display_name || f.imap_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                size="sm"
                disabled={!name.trim() || createFolder.isPending}
                onClick={handleCreate}
              >
                {createFolder.isPending ? (
                  <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <FolderPlus className="mr-1 h-3.5 w-3.5" />
                )}
                Create
              </Button>
            </div>
          </div>

          <Separator />

          <div className="flex max-h-64 flex-col gap-1 overflow-auto">
            {liveFolders.length === 0 && (
              <p className="text-sm text-muted-foreground">No folders yet.</p>
            )}
            {liveFolders.map((f) => (
              <div
                key={f.id}
                className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent"
              >
                <span className="truncate">{f.display_name || f.imap_name}</span>
                {f.special_use ? (
                  <span className="shrink-0 text-xs text-muted-foreground">Cannot be deleted</span>
                ) : (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 shrink-0 text-destructive"
                    title="Delete folder"
                    onClick={() => setPendingDelete(f)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                )}
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(next) => !next && setPendingDelete(null)}
        title={`Delete "${pendingDelete?.display_name || pendingDelete?.imap_name}"?`}
        description={
          `This destroys ${pendingDelete?.total_count ?? 0} message` +
          `${pendingDelete?.total_count === 1 ? "" : "s"} on the mail server. ` +
          "It cannot be undone."
        }
        isConfirming={deleteFolder.isPending}
        onConfirm={() => pendingDelete && handleDelete(pendingDelete)}
      />
    </>
  );
}
