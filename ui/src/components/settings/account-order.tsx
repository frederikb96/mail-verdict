"use client";

/**
 * Account display order: drag-and-drop reorderable list of accounts.
 *
 * A single, instance-wide preference -- every place that lists accounts
 * reads it through useAccounts(), which already applies it to whatever
 * the server returns, so this component only needs to save a new order.
 */

import { useState, useEffect } from "react";
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
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { Save, Loader2, GripVertical, AtSign } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import { useAccounts } from "@/hooks/use-accounts";
import { useUpdateAccountOrder } from "@/hooks/use-account-order";
import type { AccountResponse } from "@/types/api";

function SortableAccount({ account }: { account: AccountResponse }) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: account.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex items-center gap-2 rounded-md border px-3 py-2 ${
        isDragging ? "opacity-50 shadow-lg" : ""
      }`}
    >
      <button
        className="cursor-grab touch-none"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="h-4 w-4 text-muted-foreground" />
      </button>
      {account.emoji ? (
        <span className="text-sm">{account.emoji}</span>
      ) : (
        <AtSign className="h-4 w-4 text-muted-foreground" />
      )}
      <span className="flex-1 text-sm font-medium">{account.name}</span>
      <span className="text-xs text-muted-foreground">{account.imap_user}</span>
    </div>
  );
}

export function AccountOrder() {
  const { data: accounts } = useAccounts();
  const updateOrder = useUpdateAccountOrder();

  const [localOrder, setLocalOrder] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  // useAccounts() already applies the stored order, so the list it
  // returns is what the account switcher shows -- reflect that here.
  useEffect(() => {
    if (!accounts) return;
    setLocalOrder(accounts.map((account) => account.id));
    setDirty(false);
  }, [accounts]);

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    const oldIndex = localOrder.indexOf(active.id as string);
    const newIndex = localOrder.indexOf(over.id as string);
    if (oldIndex === -1 || newIndex === -1) return;

    const updated = [...localOrder];
    updated.splice(oldIndex, 1);
    updated.splice(newIndex, 0, active.id as string);
    setLocalOrder(updated);
    setDirty(true);
  };

  const handleSave = () => {
    updateOrder.mutate(localOrder, {
      onSuccess: () => setDirty(false),
    });
  };

  const accountMap = new Map<string, AccountResponse>();
  accounts?.forEach((account) => accountMap.set(account.id, account));

  if (!accounts || accounts.length < 2) {
    return null;
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <AtSign className="h-4 w-4" />
          Account Order
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-xs text-muted-foreground">
          Drag accounts to reorder the account switcher.
        </p>

        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            items={localOrder}
            strategy={verticalListSortingStrategy}
          >
            <div className="flex flex-col gap-1">
              {localOrder.map((id) => {
                const account = accountMap.get(id);
                if (!account) return null;
                return <SortableAccount key={id} account={account} />;
              })}
            </div>
          </SortableContext>
        </DndContext>

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
