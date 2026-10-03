"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, Package, Search, Star } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { OrderDetailPane } from "@/components/orders/order-detail";
import { useOrderActions } from "@/components/orders/order-actions";
import { OrderRow } from "@/components/orders/order-row";
import { useAccounts } from "@/hooks/use-accounts";
import { useOrdersList } from "@/hooks/use-orders";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  type OrderScrollAnchor,
  readOrderScrollAnchor,
  writeOrderScrollAnchor,
} from "@/lib/order-scroll-anchor";
import { stableOrder } from "@/lib/stable-order";
import type { OrderListItem } from "@/types/api";
import Link from "next/link";

const STATE_STORAGE_KEY = "mv.orders.state";

/** Every row is exactly this tall (order-row.tsx) -- what makes the list
 * and the scroll-anchor restore's arithmetic exact rather than an
 * estimate. */
const ORDER_ROW_HEIGHT = 124;

/** What the list shows: everything, only open orders, or only favorites
 * (in any state). One choice, persisted. */
type OrdersView = "all" | "open" | "favorites";

function readStoredView(): OrdersView {
  if (typeof window === "undefined") return "all";
  const stored = window.localStorage.getItem(STATE_STORAGE_KEY);
  return stored === "open" || stored === "favorites" ? stored : "all";
}

export function OrdersPage() {
  const searchParams = useSearchParams();
  const isMobile = useIsMobile();
  const [view, setView] = useState<OrdersView>("all");
  const selectedId = searchParams.get("order");

  useEffect(() => setView(readStoredView()), []);

  // The filter field: filterText is what the input shows, debouncedFilter
  // what the query asks for -- the same 150 ms the mail quick filter uses.
  const [filterText, setFilterText] = useState("");
  const [debouncedFilter, setDebouncedFilter] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedFilter(filterText), 150);
    return () => clearTimeout(timer);
  }, [filterText]);
  const q = debouncedFilter.trim();

  const { items, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage } = useOrdersList({
    state: view === "open" ? "open" : "all",
    favorites: view === "favorites",
    q,
  });
  const { data: accounts } = useAccounts();

  // The list actually rendered, decoupled from `items` (the query's own
  // latest answer) so a live update never moves a row a scrolled-down
  // reader is looking at -- see stable-order.ts. Kept in a ref alongside
  // the state so effects and event handlers always read the current
  // value without re-subscribing.
  const [shown, setShown] = useState<OrderListItem[]>([]);
  const [held, setHeld] = useState<OrderListItem[]>([]);
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const atTopRef = useRef(true);
  const isPaginatingRef = useRef(false);

  const listScrollRef = useRef<HTMLDivElement>(null);
  // The detail pane's scroll container as state rather than a ref: the
  // pane's own scroll restore needs the element during its first layout
  // pass, and a parent's ref is not attached yet while a child's layout
  // effects run (order-detail.tsx's own prop comment has the rest).
  const [detailScrollEl, setDetailScrollEl] = useState<HTMLDivElement | null>(null);

  // A new filter (view or text) is a new list, keyed on it: start over at
  // the top rather than carrying rows from the previous filter's
  // reconciled window.
  useEffect(() => {
    setShown([]);
    setHeld([]);
    atTopRef.current = true;
    listScrollRef.current?.scrollTo({ top: 0 });
  }, [view, q]);

  useEffect(() => {
    if (isLoading) return;

    if (isPaginatingRef.current) {
      isPaginatingRef.current = false;
      setShown((prev) => {
        const prevIds = new Set(prev.map((o) => o.id));
        const appended = items.filter((o) => !prevIds.has(o.id));
        return appended.length > 0 ? [...prev, ...appended] : prev;
      });
      return;
    }

    if (shownRef.current.length === 0) {
      setShown(items);
      setHeld([]);
      return;
    }

    const result = stableOrder(shownRef.current, items, atTopRef.current);
    setShown(result.rows);
    setHeld(result.held);
  }, [items, isLoading]);

  const takeOverFresh = useCallback(() => {
    setShown(itemsRef.current);
    setHeld([]);
    listScrollRef.current?.scrollTo({ top: 0 });
  }, []);

  const handleListScroll = useCallback(
    (e: React.UIEvent<HTMLDivElement>) => {
      const el = e.currentTarget;
      const isAtTop = el.scrollTop <= 2;
      const wasAtTop = atTopRef.current;
      atTopRef.current = isAtTop;
      if (isAtTop && !wasAtTop) {
        // Reaching the top is the other way to take over the fresh order,
        // besides clicking the pill.
        if (held.length > 0) takeOverFresh();
      }
      if (
        el.scrollHeight - el.scrollTop - el.clientHeight < 600 &&
        hasNextPage &&
        !isFetchingNextPage
      ) {
        isPaginatingRef.current = true;
        fetchNextPage();
      }
    },
    [held.length, takeOverFresh, hasNextPage, isFetchingNextPage, fetchNextPage],
  );

  // The one writer of this route's URL -- window.history.pushState only,
  // never router.push: the static export refetches the page's RSC
  // payload on every router navigation, even for a query-string-only
  // change, and Next's own patch over history.pushState already keeps
  // useSearchParams in step without that fetch (see this repo's notes on
  // the static-export router trap).
  const selectOrder = useCallback((id: string | null) => {
    const url = id ? `/orders?order=${id}` : "/orders";
    window.history.pushState(null, "", url);
  }, []);

  const changeView = useCallback((next: OrdersView) => {
    setView(next);
    window.localStorage.setItem(STATE_STORAGE_KEY, next);
  }, []);

  // One runner for every entry point -- row menu, swipe, detail menu --
  // with its dialogs rendered once, below.
  const { run: runOrderAction, dialogs: orderActionDialogs } = useOrderActions((id) => {
    if (id === selectedId) selectOrder(null);
  });

  // Just before a mail row hands off to the normal mail view, remember
  // where both panes sat on screen -- read back on the Back navigation
  // that returns here (order-detail.tsx's own effect does the reading).
  const beforeOpenMail = useCallback(
    (mailKey: string, rowTop: number) => {
      if (!selectedId) return;
      const index = shownRef.current.findIndex((o) => o.id === selectedId);
      const el = listScrollRef.current;
      const listRowTop = index >= 0 && el ? index * ORDER_ROW_HEIGHT - el.scrollTop : 0;
      writeOrderScrollAnchor({ orderId: selectedId, mailKey, rowTop, listIndex: index, listRowTop });
    },
    [selectedId],
  );

  // Restoring the list's own scroll position on the Back navigation that
  // returns to this order -- once per anchor (not once per selection: a
  // Back navigation can restore this same mounted page rather than
  // remounting it, so a guard keyed on selectedId alone would already
  // read as "handled" from the first visit, before any anchor existed,
  // and silently skip the real one written moments later), and only
  // once rows are actually on screen to scroll to.
  //
  // A Back navigation is not guaranteed to change selectedId or shown --
  // the router can restore this same mounted page without React seeing
  // any dependency change, so the check also runs from the ordinary
  // signals a Back navigation fires regardless of whether React
  // re-renders (order-detail.tsx's own restore effect has the identical
  // shape, for the same reason).
  const restoredAnchorKeyRef = useRef<string | null>(null);
  // The last anchor this page saw for the open order. The detail pane
  // removes the stored anchor once its own hold ends, and the list's rows
  // can arrive after that (they come from an effect, a commit later than
  // the detail's own restore), so reading storage alone would lose the
  // anchor to whichever of the two happened to be slower.
  const seenAnchorRef = useRef<OrderScrollAnchor | null>(null);
  useLayoutEffect(() => {
    const tryRestore = () => {
      if (!selectedId) return;
      const stored = readOrderScrollAnchor(selectedId);
      if (stored) seenAnchorRef.current = stored;
      const anchor =
        seenAnchorRef.current?.orderId === selectedId ? seenAnchorRef.current : null;
      if (!anchor) return;
      const anchorKey = `${anchor.orderId}:${anchor.mailKey}`;
      if (restoredAnchorKeyRef.current === anchorKey) return;
      const el = listScrollRef.current;
      if (!el || shown.length === 0) return;
      restoredAnchorKeyRef.current = anchorKey;
      const target = anchor.listIndex * ORDER_ROW_HEIGHT - anchor.listRowTop;
      const max = Math.max(0, el.scrollHeight - el.clientHeight);
      el.scrollTop = Math.max(0, Math.min(target, max));
    };

    tryRestore();
    window.addEventListener("popstate", tryRestore);
    window.addEventListener("pageshow", tryRestore);
    document.addEventListener("visibilitychange", tryRestore);
    return () => {
      window.removeEventListener("popstate", tryRestore);
      window.removeEventListener("pageshow", tryRestore);
      document.removeEventListener("visibilitychange", tryRestore);
    };
  }, [selectedId, shown]);

  const showDetailOnly = isMobile && selectedId !== null;
  const showListOnly = isMobile && selectedId === null;

  const anyAccountHasOrders = (accounts ?? []).some((a) => a.orders_enabled);
  const isEmpty = !isLoading && shown.length === 0;

  return (
    <div className="flex h-full">
      {(!isMobile || showListOnly) && (
        <div className="flex h-full w-full flex-col md:w-[400px] md:min-w-[320px] md:max-w-[480px] md:border-r">
          <div className="flex h-12 items-center border-b px-4">
            <span className="text-sm font-semibold">Orders & tickets</span>
            <div className="ml-auto flex gap-1">
              <Button
                variant={view === "all" ? "secondary" : "ghost"}
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => changeView("all")}
              >
                All
              </Button>
              <Button
                variant={view === "open" ? "secondary" : "ghost"}
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => changeView("open")}
              >
                Open
              </Button>
              <Button
                variant={view === "favorites" ? "secondary" : "ghost"}
                size="sm"
                className="h-7 px-2"
                aria-label="Favorites"
                aria-pressed={view === "favorites"}
                title="Favorites"
                onClick={() => changeView("favorites")}
              >
                <Star className="h-3.5 w-3.5" fill={view === "favorites" ? "currentColor" : "none"} />
              </Button>
            </div>
          </div>

          <div className="relative border-b px-3 py-2">
            <Search className="pointer-events-none absolute left-5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={filterText}
              onChange={(e) => setFilterText(e.target.value)}
              placeholder="Filter orders…"
              aria-label="Filter orders by merchant, subject, status or summary"
              data-testid="orders-filter"
              className="h-8 pl-7 text-sm"
              maxLength={200}
            />
          </div>

          <div className="relative flex-1 overflow-hidden">
            {held.length > 0 && (
              <button
                type="button"
                data-testid="orders-new-activity-pill"
                onClick={takeOverFresh}
                className="absolute left-1/2 top-2 z-10 inline-flex h-7 -translate-x-1/2 items-center rounded-full bg-primary px-3 text-xs text-primary-foreground shadow"
              >
                New activity
              </button>
            )}
            <div
              ref={listScrollRef}
              data-testid="orders-list-scroll"
              className="h-full overflow-y-auto [overflow-anchor:none]"
              onScroll={handleListScroll}
            >
              {isLoading &&
                Array.from({ length: 6 }).map((_, i) => (
                  <div key={i} className="h-[124px] border-b px-4 py-3">
                    <Skeleton className="h-full w-full animate-none" />
                  </div>
                ))}

              {isEmpty && (q !== "" || view === "favorites") && (
                <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
                  <Package className="h-12 w-12 opacity-40" />
                  <p className="text-sm font-medium">
                    {q !== "" ? "No matching orders" : "No favorites yet"}
                  </p>
                  {q === "" && (
                    <p className="max-w-[320px] text-sm text-muted-foreground">
                      Mark an order as favorite from its menu or the detail's top-right menu.
                    </p>
                  )}
                </div>
              )}

              {isEmpty && q === "" && view !== "favorites" && !anyAccountHasOrders && (
                <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
                  <Package className="h-12 w-12 opacity-40" />
                  <p className="text-sm font-medium">No orders yet</p>
                  <p className="max-w-[320px] text-sm text-muted-foreground">
                    Mail about purchases, tickets and bookings is bundled here. Switch it on for
                    an account first.
                  </p>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-2"
                    render={<Link href="/accounts" />}
                  >
                    Open accounts
                  </Button>
                </div>
              )}

              {isEmpty && q === "" && view !== "favorites" && anyAccountHasOrders && (
                <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
                  <Package className="h-12 w-12 opacity-40" />
                  <p className="text-sm font-medium">Nothing bundled yet</p>
                  <p className="max-w-[320px] text-sm text-muted-foreground">
                    Order mail appears here within a minute of arriving.
                  </p>
                </div>
              )}

              {shown.map((order) => (
                <OrderRow
                  key={order.id}
                  order={order}
                  selected={order.id === selectedId}
                  onSelect={() => selectOrder(order.id)}
                  onAction={runOrderAction}
                />
              ))}
            </div>
          </div>
        </div>
      )}

      {(!isMobile || showDetailOnly) && (
        <div
          ref={setDetailScrollEl}
          data-testid="order-detail-scroll"
          className="flex-1 overflow-y-auto [overflow-anchor:none]"
        >
          {showDetailOnly && (
            <div className="flex h-11 items-center gap-2 border-b px-2">
              <Button variant="ghost" size="sm" onClick={() => selectOrder(null)}>
                <ArrowLeft className="h-4 w-4" />
                Orders
              </Button>
            </div>
          )}
          {selectedId ? (
            <OrderDetailPane
              orderId={selectedId}
              onBack={showDetailOnly ? undefined : () => selectOrder(null)}
              onAction={runOrderAction}
              scrollContainer={detailScrollEl}
              onBeforeOpenMail={beforeOpenMail}
            />
          ) : (
            !isMobile && (
              <div className="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground">
                <Package className="h-16 w-16 opacity-30" />
                <p>Select an order</p>
              </div>
            )
          )}
        </div>
      )}
      {orderActionDialogs}
    </div>
  );
}
