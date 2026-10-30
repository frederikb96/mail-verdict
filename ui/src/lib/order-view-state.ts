/**
 * The orders list's view: which kind it shows plus the Open and Favorites
 * toggles, persisted as one small object. Pure parsing and storage access
 * so the shapes are unit tested without a browser -- see
 * order-view-state.test.ts.
 */

export type OrderKindFilter = "all" | "order" | "ticket";

export interface OrdersView {
  kind: OrderKindFilter;
  /** Only entries still open. */
  open: boolean;
  /** Only favorites, in any state. */
  favorites: boolean;
}

export const ORDERS_VIEW_KEY = "mv.orders.view";

/** The earlier single-choice key ("all" | "open" | "favorites"). Read once
 * when no view is stored yet, removed on the first write. */
export const LEGACY_STATE_KEY = "mv.orders.state";

export const DEFAULT_ORDERS_VIEW: OrdersView = { kind: "all", open: false, favorites: false };

type ReadableStorage = Pick<Storage, "getItem">;
type WritableStorage = Pick<Storage, "setItem" | "removeItem">;

export function parseOrdersView(raw: string | null, legacy: string | null): OrdersView {
  if (raw !== null) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed !== null && typeof parsed === "object") {
        const record = parsed as Record<string, unknown>;
        return {
          kind: record.kind === "order" || record.kind === "ticket" ? record.kind : "all",
          open: record.open === true,
          favorites: record.favorites === true,
        };
      }
    } catch {
      // Not JSON -- fall through to the default.
    }
    return DEFAULT_ORDERS_VIEW;
  }
  return {
    ...DEFAULT_ORDERS_VIEW,
    open: legacy === "open",
    favorites: legacy === "favorites",
  };
}

export function readOrdersView(storage: ReadableStorage): OrdersView {
  try {
    return parseOrdersView(storage.getItem(ORDERS_VIEW_KEY), storage.getItem(LEGACY_STATE_KEY));
  } catch {
    return DEFAULT_ORDERS_VIEW;
  }
}

export function writeOrdersView(storage: WritableStorage, view: OrdersView): void {
  try {
    storage.setItem(ORDERS_VIEW_KEY, JSON.stringify(view));
    storage.removeItem(LEGACY_STATE_KEY);
  } catch {
    // Private browsing or a full quota: the choice just does not persist.
  }
}
