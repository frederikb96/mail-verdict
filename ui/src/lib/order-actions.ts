/**
 * The one list of things that can be done to an order. The detail pane's
 * menu, the row's right-click / long-press menu and the row's swipes all
 * read it, so the three entry points cannot drift apart.
 *
 * Pure, so the list for a given order state is unit tested without React --
 * see order-actions.test.ts.
 */

import type { OrderKind, OrderListItem, OrderUpdateRequest } from "@/types/api";

export type OrderKindActionId = "kind_ticket" | "kind_order" | "kind_other";

export type OrderActionId =
  | "favorite"
  | "close"
  | "seal"
  | OrderKindActionId
  | "rewrite"
  | "merge"
  | "delete";

export type OrderFlags = Pick<OrderListItem, "is_favorite" | "is_open" | "is_sealed" | "kind">;

export interface OrderActionEntry {
  id: OrderActionId;
  label: string;
  /** A second, muted line under the label. */
  hint?: string;
  destructive?: boolean;
  /** A separator is drawn above this entry. */
  separatorBefore?: boolean;
}

const KIND_ACTIONS: { id: OrderKindActionId; kind: OrderKind; label: string }[] = [
  { id: "kind_ticket", kind: "ticket", label: "Mark as ticket" },
  { id: "kind_order", kind: "order", label: "Mark as order" },
  { id: "kind_other", kind: "other", label: "Mark as other" },
];

export function orderActions(order: OrderFlags): OrderActionEntry[] {
  const kindEntries: OrderActionEntry[] = KIND_ACTIONS.filter((a) => a.kind !== order.kind).map(
    ({ id, label }, i) => ({ id, label, separatorBefore: i === 0 }),
  );
  return [
    { id: "favorite", label: order.is_favorite ? "Unfavorite" : "Favorite" },
    { id: "close", label: order.is_open ? "Close" : "Reopen" },
    {
      id: "seal",
      label: order.is_sealed ? "Unseal" : "Seal",
      hint: order.is_sealed ? "Mail is added again" : "Add no more mail",
    },
    ...kindEntries,
    { id: "rewrite", label: "Rewrite summary", separatorBefore: true },
    { id: "merge", label: "Merge into another order…" },
    { id: "delete", label: "Delete order…", destructive: true, separatorBefore: true },
  ];
}

export interface OrderToggle {
  patch: OrderUpdateRequest;
  /** What the confirmation toast says once the change has landed. */
  message: string;
}

/** The PATCH an action amounts to, or null for the actions that are not a
 * flag flip (rewrite, merge, delete). Closing sends `is_open: false`, so a
 * person's decision is recorded as theirs by the server. */
export function orderToggle(order: OrderFlags, id: OrderActionId): OrderToggle | null {
  switch (id) {
    case "favorite":
      return order.is_favorite
        ? { patch: { is_favorite: false }, message: "Removed from favorites" }
        : { patch: { is_favorite: true }, message: "Added to favorites" };
    case "close":
      return order.is_open
        ? { patch: { is_open: false }, message: "Order closed" }
        : { patch: { is_open: true }, message: "Order reopened" };
    case "seal":
      return order.is_sealed
        ? { patch: { is_sealed: false }, message: "Order unsealed" }
        : { patch: { is_sealed: true }, message: "Order sealed" };
    case "kind_ticket":
    case "kind_order":
    case "kind_other": {
      const kind = KIND_ACTIONS.find((a) => a.id === id)!.kind;
      return { patch: { kind }, message: `Marked as ${kind}` };
    }
    default:
      return null;
  }
}
