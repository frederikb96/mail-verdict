"use client";

import { Fragment, useCallback, useState } from "react";
import {
  CircleCheck,
  GitMerge,
  Lock,
  LockOpen,
  RefreshCw,
  RotateCcw,
  Star,
  Trash2,
} from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { OrderPickerDialog } from "@/components/orders/order-picker-dialog";
import { useDeleteOrder, useMergeOrder, useRewriteOrder, useUpdateOrder } from "@/hooks/use-orders";
import { useToast } from "@/hooks/use-toast";
import { type OrderActionId, orderActions, orderToggle } from "@/lib/order-actions";
import type { OrderListItem } from "@/types/api";

function ActionIcon({ id, order }: { id: OrderActionId; order: OrderListItem }) {
  const cls = "h-4 w-4";
  switch (id) {
    case "favorite":
      return <Star className={cls} fill={order.is_favorite ? "currentColor" : "none"} />;
    case "close":
      return order.is_open ? <CircleCheck className={cls} /> : <RotateCcw className={cls} />;
    case "seal":
      return order.is_sealed ? <LockOpen className={cls} /> : <Lock className={cls} />;
    case "rewrite":
      return <RefreshCw className={cls} />;
    case "merge":
      return <GitMerge className={cls} />;
    case "delete":
      return <Trash2 className={cls} />;
  }
}

/** The menu items for one order, from the single action list
 * (lib/order-actions.ts). Rendered inside the detail's dropdown and the
 * row's context menu alike -- both popups are the same Base UI menu. */
export function OrderMenuItems({
  order,
  onAction,
}: {
  order: OrderListItem;
  onAction: (order: OrderListItem, id: OrderActionId) => void;
}) {
  return (
    <>
      {orderActions(order).map((entry) => (
        <Fragment key={entry.id}>
          {entry.separatorBefore && <DropdownMenuSeparator />}
          <DropdownMenuItem
            variant={entry.destructive ? "destructive" : "default"}
            onClick={() => onAction(order, entry.id)}
          >
            <ActionIcon id={entry.id} order={order} />
            <span className="flex flex-col">
              <span>{entry.label}</span>
              {entry.hint && (
                <span className="text-xs font-normal text-muted-foreground">{entry.hint}</span>
              )}
            </span>
          </DropdownMenuItem>
        </Fragment>
      ))}
    </>
  );
}

/** Runs any action on any order, and owns the dialogs the heavier ones
 * need (merge target, confirmations) -- so the detail's menu, a row's
 * menu and a swipe all reach the same code and the same dialogs.
 *
 * `onRemoved` hears about an order that is gone afterwards (deleted or
 * merged away), so the page can leave it if it was the open one. */
export function useOrderActions(onRemoved: (orderId: string) => void) {
  const { push: pushToast } = useToast();
  const updateOrder = useUpdateOrder();
  const rewriteOrder = useRewriteOrder();
  const deleteOrder = useDeleteOrder();
  const mergeOrder = useMergeOrder();

  const [confirmDelete, setConfirmDelete] = useState<OrderListItem | null>(null);
  const [pickingMergeFor, setPickingMergeFor] = useState<OrderListItem | null>(null);
  const [confirmMerge, setConfirmMerge] = useState<{ order: OrderListItem; into: string } | null>(null);

  const run = useCallback(
    (order: OrderListItem, id: OrderActionId) => {
      const toggle = orderToggle(order, id);
      if (toggle) {
        updateOrder.mutate(
          { id: order.id, patch: toggle.patch },
          {
            onSuccess: () => pushToast(toggle.message, "success", 2500),
            onError: () => pushToast("Could not update the order", "error"),
          },
        );
        return;
      }
      if (id === "rewrite") rewriteOrder.mutate(order.id);
      else if (id === "merge") setPickingMergeFor(order);
      else if (id === "delete") setConfirmDelete(order);
    },
    [updateOrder, rewriteOrder, pushToast],
  );

  const plural = (n: number) => `${n} mail${n === 1 ? "" : "s"}`;

  const dialogs = (
    <>
      <ConfirmDialog
        open={confirmDelete !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmDelete(null);
        }}
        title="Delete this order?"
        description={`Its ${plural(confirmDelete?.mail_count ?? 0)} stay where they are.`}
        confirmLabel="Delete order"
        onConfirm={() => {
          const target = confirmDelete;
          setConfirmDelete(null);
          if (target) {
            deleteOrder.mutate(target.id);
            onRemoved(target.id);
          }
        }}
      />

      <OrderPickerDialog
        open={pickingMergeFor !== null}
        onOpenChange={(open) => {
          if (!open) setPickingMergeFor(null);
        }}
        excludeId={pickingMergeFor?.id}
        onChoose={(into) => {
          if (pickingMergeFor) setConfirmMerge({ order: pickingMergeFor, into });
          setPickingMergeFor(null);
        }}
      />
      <ConfirmDialog
        open={confirmMerge !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmMerge(null);
        }}
        title="Merge this order into the chosen one?"
        description={`Its ${plural(confirmMerge?.order.mail_count ?? 0)} move over and this order disappears.`}
        confirmLabel="Merge"
        onConfirm={() => {
          const pending = confirmMerge;
          setConfirmMerge(null);
          if (pending) {
            mergeOrder.mutate({ id: pending.order.id, into: pending.into });
            onRemoved(pending.order.id);
          }
        }}
      />
    </>
  );

  return { run, dialogs };
}
