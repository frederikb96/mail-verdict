"use client";

/**
 * Unified views setup, in one card: the views themselves (name, emoji,
 * sidebar order) and which folders, from any account, each one shows. A
 * folder can belong to several views, chosen per folder with a
 * multi-select. EmojiPicker is shared with the account cards.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useAtom } from "jotai";
import { ChevronDown, GripVertical, Layers, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";

import { useAccounts } from "@/hooks/use-accounts";
import { useFolders } from "@/hooks/use-folders";
import {
  useCreateUnifiedView,
  useDeleteUnifiedView,
  useSetFolderViews,
  useUnifiedFolders,
  useUpdateUnifiedFolderOrder,
  useUpdateUnifiedView,
} from "@/hooks/use-unified-view";
import { ApiError } from "@/lib/api";
import { selectedUnifiedFolderAtom } from "@/lib/atoms";
import type { AccountResponse, FolderResponse, UnifiedFolderResponse } from "@/types/api";

const COMMON_EMOJIS = [
  "\u{1F4E7}", "\u{1F4E8}", "\u{1F4E9}", "\u{1F4EC}", "\u{1F4ED}",
  "\u{1F4EE}", "\u{1F4F0}", "\u{1F3E2}", "\u{1F3E0}", "\u{1F393}",
  "\u{1F4BC}", "\u{1F3AF}", "\u{2B50}", "\u{1F525}", "\u{1F680}",
  "\u{1F308}", "\u{1F30D}", "\u{2764}\u{FE0F}", "\u{1F4A1}", "\u{1F50D}",
  "\u{1F512}", "\u{1F511}", "\u{2699}\u{FE0F}", "\u{1F3F7}\u{FE0F}", "\u{1F4CC}",
  "\u{2705}", "\u{274C}", "\u{26A0}\u{FE0F}", "\u{2603}\u{FE0F}", "\u{1F31F}",
  "\u{1F535}", "\u{1F534}", "\u{1F7E2}", "\u{1F7E1}", "\u{1F7E3}",
];

export function EmojiPicker({
  currentEmoji,
  onSelect,
  label = "Choose emoji",
}: {
  currentEmoji: string | null;
  onSelect: (emoji: string | null) => void;
  label?: string;
}) {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <div className="relative">
      <button
        type="button"
        className="flex h-9 w-9 items-center justify-center rounded-md border bg-background text-lg hover:bg-accent"
        onClick={() => setIsOpen(!isOpen)}
        title={label}
        aria-label={label}
      >
        {currentEmoji || "\u{2795}"}
      </button>
      {isOpen && (
        <div className="absolute left-0 top-10 z-50 grid grid-cols-7 gap-1 rounded-md border bg-popover p-2 shadow-md">
          {currentEmoji && (
            <button
              type="button"
              className="flex h-8 w-8 items-center justify-center rounded text-sm hover:bg-accent"
              onClick={() => {
                onSelect(null);
                setIsOpen(false);
              }}
              title="Clear emoji"
            >
              {"\u{274C}"}
            </button>
          )}
          {COMMON_EMOJIS.map((emoji) => (
            <button
              key={emoji}
              type="button"
              className="flex h-8 w-8 items-center justify-center rounded text-lg hover:bg-accent"
              onClick={() => {
                onSelect(emoji);
                setIsOpen(false);
              }}
            >
              {emoji}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ViewRow({ view }: { view: UnifiedFolderResponse }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: view.id });
  const updateView = useUpdateUnifiedView();
  const deleteView = useDeleteUnifiedView();
  const [selectedUnifiedFolder, setSelectedUnifiedFolder] = useAtom(selectedUnifiedFolderAtom);
  const [name, setName] = useState(view.unified_name);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setName(view.unified_name);
  }, [view.unified_name]);

  const commitName = () => {
    const next = name.trim();
    const previous = view.unified_name;
    if (!next || next === previous) {
      setName(previous);
      return;
    }
    updateView.mutate(
      { viewId: view.id, data: { name: next } },
      {
        // The open view is addressed by its name -- follow the rename.
        onSuccess: () => {
          if (selectedUnifiedFolder === previous) setSelectedUnifiedFolder(next);
        },
        onError: () => setName(previous),
      },
    );
  };

  const folderCount = view.folders.length;

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      data-testid="unified-view-row"
      data-view-id={view.id}
      className={`flex items-center gap-2 rounded-md border px-2 py-1.5 ${
        isDragging ? "opacity-50 shadow-lg" : ""
      }`}
    >
      <button
        type="button"
        className="cursor-grab touch-none"
        aria-label={`Reorder ${view.unified_name}`}
        {...attributes}
        {...listeners}
      >
        <GripVertical className="h-4 w-4 text-muted-foreground" />
      </button>
      <EmojiPicker
        currentEmoji={view.emoji}
        label={`Icon for ${view.unified_name}`}
        onSelect={(emoji) => updateView.mutate({ viewId: view.id, data: { emoji } })}
      />
      <Input
        value={name}
        onChange={(e) => setName(e.target.value)}
        onBlur={commitName}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") setName(view.unified_name);
        }}
        aria-label={`Name of the ${view.unified_name} view`}
        className="h-8 min-w-0 flex-1"
      />
      <span className="shrink-0 text-xs text-muted-foreground">
        {folderCount} folder{folderCount === 1 ? "" : "s"}
      </span>
      <Button
        variant="ghost"
        size="icon"
        className="h-8 w-8 shrink-0"
        aria-label={`Delete the ${view.unified_name} view`}
        title="Delete view"
        onClick={() => setConfirmDelete(true)}
      >
        <Trash2 className="h-4 w-4" />
      </Button>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete the "${view.unified_name}" view?`}
        description="Only the view goes. Its folders and every message in them stay exactly where they are."
        confirmLabel="Delete view"
        confirmVariant="destructive"
        isConfirming={deleteView.isPending}
        onConfirm={() => deleteView.mutate(view.id, { onSuccess: () => setConfirmDelete(false) })}
      />
    </div>
  );
}

function CreateViewForm() {
  const createView = useCreateUnifiedView();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  // isPending is a render snapshot -- a fast double Enter would reach the
  // handler twice before it flips. The ref is the guard.
  const submittingRef = useRef(false);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || submittingRef.current) return;
    submittingRef.current = true;
    createView.mutate(
      { name: trimmed },
      {
        onSuccess: () => {
          setName("");
          setError(null);
        },
        onError: (err) =>
          setError(
            err instanceof ApiError && err.status === 409
              ? `A view named "${trimmed}" already exists`
              : "The view could not be created",
          ),
        onSettled: () => {
          submittingRef.current = false;
        },
      },
    );
  };

  return (
    <form onSubmit={submit} className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="New view name"
          aria-label="New unified view name"
          className="h-8 min-w-0 flex-1"
        />
        <Button type="submit" size="sm" disabled={!name.trim() || createView.isPending}>
          <Plus className="mr-1 h-3 w-3" />
          Add view
        </Button>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
    </form>
  );
}

/** Which views one folder belongs to -- a multi-select that stays open
 * while ticking several, each tick saving the folder's complete set. */
function FolderViewsSelect({
  folder,
  label,
  views,
}: {
  folder: FolderResponse;
  label: string;
  views: UnifiedFolderResponse[];
}) {
  const setFolderViews = useSetFolderViews();
  const [ids, setIds] = useState<string[]>(folder.unified_view_ids);
  // Mirrors `ids` synchronously, so two ticks landing before a re-render
  // both build on the latest set rather than on the same stale one.
  const idsRef = useRef(ids);
  const serverKey = folder.unified_view_ids.join(",");

  useEffect(() => {
    idsRef.current = folder.unified_view_ids;
    setIds(folder.unified_view_ids);
    // Keyed on the value, not the array's identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey]);

  const toggle = (viewId: string, checked: boolean) => {
    const wanted = new Set(idsRef.current);
    if (checked) wanted.add(viewId);
    else wanted.delete(viewId);
    const next = views.map((v) => v.id).filter((id) => wanted.has(id));
    idsRef.current = next;
    setIds(next);
    setFolderViews.mutate(
      { folderId: folder.id, viewIds: next },
      {
        onError: () => {
          idsRef.current = folder.unified_view_ids;
          setIds(folder.unified_view_ids);
        },
      },
    );
  };

  const chosen = views.filter((v) => ids.includes(v.id));

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="outline"
            size="sm"
            className="h-auto min-h-8 min-w-0 max-w-[65%] justify-between gap-2 py-1"
            aria-label={`Unified views for ${label}`}
          />
        }
      >
        <span className="flex min-w-0 flex-wrap items-center gap-1">
          {chosen.length === 0 ? (
            <span className="text-xs text-muted-foreground">No view</span>
          ) : (
            chosen.map((v) => (
              <span
                key={v.id}
                data-testid="folder-view-chip"
                className="inline-flex max-w-full items-center gap-1 truncate rounded-full bg-muted px-1.5 text-xs"
              >
                {v.emoji && <span aria-hidden>{v.emoji}</span>}
                <span className="truncate">{v.unified_name}</span>
              </span>
            ))
          )}
        </span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 opacity-50" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        {views.map((v) => (
          <DropdownMenuCheckboxItem
            key={v.id}
            checked={ids.includes(v.id)}
            onCheckedChange={(checked) => toggle(v.id, checked)}
          >
            {v.emoji && <span aria-hidden>{v.emoji}</span>}
            {v.unified_name}
          </DropdownMenuCheckboxItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

const SPECIAL_USE_ORDER = ["inbox", "drafts", "sent", "archive", "junk", "trash"];

function folderRank(folder: FolderResponse): number {
  const index = folder.special_use ? SPECIAL_USE_ORDER.indexOf(folder.special_use) : -1;
  return index === -1 ? SPECIAL_USE_ORDER.length : index;
}

function AccountFolders({
  account,
  views,
}: {
  account: AccountResponse;
  views: UnifiedFolderResponse[];
}) {
  const { data: folders } = useFolders(account.id);
  const ordered = useMemo(
    () =>
      [...(folders ?? [])].sort(
        (a, b) => folderRank(a) - folderRank(b) || a.imap_name.localeCompare(b.imap_name),
      ),
    [folders],
  );

  return (
    <div
      className="flex flex-col gap-1.5"
      data-testid="unified-account-folders"
      data-account-id={account.id}
    >
      <div className="flex items-center gap-2 text-sm font-medium">
        {account.emoji && <span aria-hidden>{account.emoji}</span>}
        <span className="truncate">{account.name}</span>
      </div>
      {ordered.map((folder) => {
        const label = folder.display_name || folder.imap_name;
        return (
          <div
            key={folder.id}
            data-testid="unified-folder-row"
            data-folder-id={folder.id}
            className="flex items-center justify-between gap-3 pl-1"
          >
            <span className="min-w-0 truncate text-sm text-muted-foreground">{label}</span>
            <FolderViewsSelect folder={folder} label={label} views={views} />
          </div>
        );
      })}
      {folders && folders.length === 0 && (
        <span className="pl-1 text-sm text-muted-foreground">No folders synced yet</span>
      )}
    </div>
  );
}

export function UnifiedViewsSettings() {
  const { data: views } = useUnifiedFolders();
  const { data: accounts } = useAccounts();
  const updateOrder = useUpdateUnifiedFolderOrder();

  const byId = useMemo(() => new Map((views ?? []).map((v) => [v.id, v])), [views]);
  const serverOrder = useMemo(() => (views ?? []).map((v) => v.id), [views]);
  const serverKey = serverOrder.join(",");
  const [order, setOrder] = useState<string[]>(serverOrder);

  useEffect(() => {
    setOrder(serverOrder);
    // Keyed on the value, not the array's identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey]);

  const shown = [
    ...order.filter((id) => byId.has(id)),
    ...serverOrder.filter((id) => !order.includes(id)),
  ];

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const from = shown.indexOf(String(active.id));
    const to = shown.indexOf(String(over.id));
    if (from === -1 || to === -1) return;
    const next = arrayMove(shown, from, to);
    setOrder(next);
    updateOrder.mutate(next.map((id) => byId.get(id)!.unified_name));
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Layers className="h-4 w-4" />
          Unified views
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-xs text-muted-foreground">
          A unified view shows the mail of the folders you choose, from any account, as one list.
          A folder can belong to several views. Drag a view to reorder the sidebar.
        </p>

        {shown.length > 0 && (
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
            <SortableContext items={shown} strategy={verticalListSortingStrategy}>
              <div className="flex flex-col gap-1">
                {shown.map((id) => (
                  <ViewRow key={id} view={byId.get(id)!} />
                ))}
              </div>
            </SortableContext>
          </DndContext>
        )}

        <CreateViewForm />

        {views && views.length > 0 && (
          <div className="flex flex-col gap-4 border-t pt-4">
            <p className="text-xs font-medium text-muted-foreground">
              Which views each folder belongs to
            </p>
            {accounts?.map((account) => (
              <AccountFolders key={account.id} account={account} views={views} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
