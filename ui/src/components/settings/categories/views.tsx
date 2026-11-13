"use client";

/**
 * Unified views: a list of views, each opening in place to pick its folders
 * from any account. A tick writes at once; a folder keeps the other views
 * it belongs to.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
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
import { useQueryClient } from "@tanstack/react-query";
import { useAtom } from "jotai";
import { GripVertical, Layers, MoreVertical, Plus } from "lucide-react";

import { EmojiPicker } from "@/components/common/emoji-picker";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { CategoryHeader, SavedNote } from "@/components/settings/blocks";
import { FolderPicker } from "@/components/settings/folder-picker";
import { useAccounts } from "@/hooks/use-accounts";
import { folderKeys } from "@/hooks/use-folders";
import { refreshMailViews } from "@/hooks/use-mails";
import { useSearchFolders } from "@/hooks/use-search-folders";
import {
  useCreateUnifiedView,
  useDeleteUnifiedView,
  useUnifiedFolders,
  useUpdateUnifiedFolderOrder,
  useUpdateUnifiedView,
} from "@/hooks/use-unified-view";
import { api, ApiError } from "@/lib/api";
import { selectedUnifiedFolderAtom } from "@/lib/atoms";
import { cn } from "@/lib/utils";
import type { FolderResponse, UnifiedFolderResponse } from "@/types/api";

type SaveState = "idle" | "saving" | "saved" | "error";

function memberFolderIds(view: UnifiedFolderResponse): string[] {
  return view.folders.map((f) => f.folder_id);
}

/**
 * The folder picker of one view, writing as ticks arrive.
 *
 * A view's membership is stored per folder (the folder's whole set of
 * views), so a change is a diff against what the server holds: one write per
 * folder that joined or left. Ticks landing while a write is out are folded
 * into the newest wanted set and applied after it, so writes never overtake
 * one another.
 */
function ViewFolders({ view }: { view: UnifiedFolderResponse }) {
  const qc = useQueryClient();
  const { data: accounts } = useAccounts();
  const { options } = useSearchFolders();
  const serverIds = memberFolderIds(view);
  const serverKey = serverIds.join(",");
  const [ids, setIds] = useState(serverIds);
  const [state, setState] = useState<SaveState>("idle");
  const confirmedRef = useRef(serverIds);
  const wantedRef = useRef<string[] | null>(null);
  const runningRef = useRef(false);
  const accountOfFolder = useMemo(
    () => new Map(options.map((o) => [o.folder.id, o.accountId])),
    [options],
  );

  useEffect(() => {
    // A refetch landing mid-write describes a set the reader has moved past.
    if (runningRef.current || wantedRef.current) return;
    confirmedRef.current = serverIds;
    setIds(serverIds);
    // Keyed on the value, not the array's identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey]);

  const currentViewIds = (folderId: string): string[] => {
    const accountId = accountOfFolder.get(folderId);
    const folders = accountId
      ? qc.getQueryData<FolderResponse[]>(folderKeys.list(accountId))
      : undefined;
    return folders?.find((f) => f.id === folderId)?.unified_view_ids ?? [];
  };

  const flush = async () => {
    if (runningRef.current) return;
    runningRef.current = true;
    setState("saving");
    try {
      while (wantedRef.current) {
        const target = wantedRef.current;
        wantedRef.current = null;
        const before = new Set(confirmedRef.current);
        const after = new Set(target);
        const changes = [
          ...target.filter((id) => !before.has(id)).map((id) => ({ id, join: true })),
          ...confirmedRef.current.filter((id) => !after.has(id)).map((id) => ({ id, join: false })),
        ];
        for (const { id, join } of changes) {
          const others = currentViewIds(id).filter((v) => v !== view.id);
          await api.folders.updatePrefs(id, {
            unified_view_ids: join ? [...others, view.id] : others,
          });
        }
        confirmedRef.current = target;
      }
      setState("saved");
    } catch {
      wantedRef.current = null;
      setIds(confirmedRef.current);
      setState("error");
    } finally {
      runningRef.current = false;
      refreshMailViews(qc);
    }
  };

  const change = (next: string[]) => {
    setIds(next);
    wantedRef.current = next;
    void flush();
  };

  return (
    <div className="flex flex-col gap-2 border-t px-3 py-3">
      <FolderPicker value={ids} onChange={change} accounts={(accounts ?? []).filter((a) => a.is_active)} />
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        Changes save as you tick. <SavedNote state={state} />
      </div>
    </div>
  );
}

function ViewRow({
  view,
  expanded,
  onToggle,
  onMove,
  canMoveUp,
  canMoveDown,
}: {
  view: UnifiedFolderResponse;
  expanded: boolean;
  onToggle: () => void;
  onMove: (direction: -1 | 1) => void;
  canMoveUp: boolean;
  canMoveDown: boolean;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: view.id,
  });
  const updateView = useUpdateUnifiedView();
  const deleteView = useDeleteUnifiedView();
  const [selectedUnifiedFolder, setSelectedUnifiedFolder] = useAtom(selectedUnifiedFolderAtom);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(view.unified_name);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setName(view.unified_name);
  }, [view.unified_name]);

  const commitName = () => {
    const next = name.trim();
    const previous = view.unified_name;
    setRenaming(false);
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
  const accountCount = new Set(view.folders.map((f) => f.account_id)).size;
  const meta =
    folderCount === 0
      ? "no folders yet"
      : `${folderCount} folder${folderCount === 1 ? "" : "s"} · ${accountCount} account${accountCount === 1 ? "" : "s"}`;

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      data-testid="unified-view-row"
      data-view-id={view.id}
      className={cn(
        "rounded-lg border bg-card",
        expanded && "border-foreground/40",
        isDragging && "opacity-50 shadow-lg",
      )}
    >
      <div className="flex items-center gap-2 px-2 py-1.5">
        <button
          type="button"
          className="hidden cursor-grab touch-none pointer-fine:block"
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
        {renaming ? (
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={commitName}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
              if (e.key === "Escape") {
                setName(view.unified_name);
                setRenaming(false);
              }
            }}
            aria-label={`Name of the ${view.unified_name} view`}
            className="h-8 min-w-0 flex-1"
          />
        ) : (
          <button
            type="button"
            aria-expanded={expanded}
            onClick={onToggle}
            className="flex min-w-0 flex-1 flex-col items-start py-0.5 text-left sm:flex-row sm:items-center sm:gap-3"
          >
            <span className="max-w-full truncate text-sm font-medium">{view.unified_name}</span>
            <span className="text-xs text-muted-foreground sm:ml-auto">{meta}</span>
          </button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 shrink-0"
                aria-label={`Actions for ${view.unified_name}`}
              />
            }
          >
            <MoreVertical className="h-4 w-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={() => setRenaming(true)}>Rename</DropdownMenuItem>
            <DropdownMenuItem disabled={!canMoveUp} onClick={() => onMove(-1)}>
              Move up
            </DropdownMenuItem>
            <DropdownMenuItem disabled={!canMoveDown} onClick={() => onMove(1)}>
              Move down
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onClick={() => setConfirmDelete(true)}>
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {expanded && <ViewFolders view={view} />}
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

function NewViewForm({ onDone }: { onDone: () => void }) {
  const createView = useCreateUnifiedView();
  const [name, setName] = useState("");
  const [emoji, setEmoji] = useState<string | null>(null);
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
      { name: trimmed, emoji },
      {
        onSuccess: () => onDone(),
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
    <form
      onSubmit={submit}
      onKeyDown={(e) => {
        if (e.key === "Escape") onDone();
      }}
      className="flex flex-col gap-1 rounded-lg border border-dashed p-2"
    >
      <div className="flex items-center gap-2">
        <EmojiPicker currentEmoji={emoji} onSelect={setEmoji} label="Icon for the new view" />
        <Input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Name of the new view"
          aria-label="New unified view name"
          className="h-8 min-w-0 flex-1"
        />
        <Button type="submit" size="sm" disabled={!name.trim() || createView.isPending}>
          Create
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onDone}>
          Cancel
        </Button>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
    </form>
  );
}

export function ViewsSettings() {
  const { data: views, isLoading } = useUnifiedFolders();
  const updateOrder = useUpdateUnifiedFolderOrder();
  const [creating, setCreating] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

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

  const reorder = (from: number, to: number) => {
    if (from === -1 || to < 0 || to >= shown.length) return;
    const next = arrayMove(shown, from, to);
    setOrder(next);
    updateOrder.mutate(next.map((id) => byId.get(id)!.unified_name));
  };

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    reorder(shown.indexOf(String(active.id)), shown.indexOf(String(over.id)));
  };

  return (
    <>
      <CategoryHeader
        title="Unified views"
        description="A view merges the folders you pick, from any account, into one list in the sidebar. Open a view to choose its folders; drag it, or use its menu, to reorder the sidebar."
        actions={
          <Button size="sm" onClick={() => setCreating(true)} disabled={creating}>
            <Plus className="mr-1 h-3.5 w-3.5" />
            New view
          </Button>
        }
      />

      {creating && <NewViewForm onDone={() => setCreating(false)} />}

      {isLoading && (
        <div className="flex flex-col gap-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      )}

      {!isLoading && shown.length === 0 && !creating && (
        <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
          <Layers className="mx-auto mb-2 h-6 w-6 opacity-60" />
          No unified views yet. A view merges folders from any account into one list.
        </div>
      )}

      {shown.length > 0 && (
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <SortableContext items={shown} strategy={verticalListSortingStrategy}>
            <div className="flex flex-col gap-2">
              {shown.map((id, index) => (
                <ViewRow
                  key={id}
                  view={byId.get(id)!}
                  expanded={expandedId === id}
                  onToggle={() => setExpandedId(expandedId === id ? null : id)}
                  onMove={(direction) => reorder(index, index + direction)}
                  canMoveUp={index > 0}
                  canMoveDown={index < shown.length - 1}
                />
              ))}
            </div>
          </SortableContext>
        </DndContext>
      )}
    </>
  );
}
