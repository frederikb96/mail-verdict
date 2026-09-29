"use client";

import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, Package } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { OrderDetailPane } from "@/components/orders/order-detail";
import { OrderRow } from "@/components/orders/order-row";
import { useOrdersList } from "@/hooks/use-orders";
import { useIsMobile } from "@/hooks/use-mobile";
import Link from "next/link";

const STATE_STORAGE_KEY = "mv.orders.state";

function readStoredState(): "all" | "open" {
  if (typeof window === "undefined") return "all";
  return window.localStorage.getItem(STATE_STORAGE_KEY) === "open" ? "open" : "all";
}

export function OrdersPage() {
  const searchParams = useSearchParams();
  const isMobile = useIsMobile();
  const [state, setState] = useState<"all" | "open">("all");
  const selectedId = searchParams.get("order");

  useEffect(() => setState(readStoredState()), []);

  const { items, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage } =
    useOrdersList(state);

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

  const changeState = useCallback((next: "all" | "open") => {
    setState(next);
    window.localStorage.setItem(STATE_STORAGE_KEY, next);
  }, []);

  const showDetailOnly = isMobile && selectedId !== null;
  const showListOnly = isMobile && selectedId === null;

  return (
    <div className="flex h-full">
      {(!isMobile || showListOnly) && (
        <div className="flex h-full w-full flex-col md:w-[400px] md:min-w-[320px] md:max-w-[480px] md:border-r">
          <div className="flex h-12 items-center border-b px-4">
            <span className="text-sm font-semibold">Orders & tickets</span>
            <div className="ml-auto flex gap-1">
              <Button
                variant={state === "all" ? "secondary" : "ghost"}
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => changeState("all")}
              >
                All
              </Button>
              <Button
                variant={state === "open" ? "secondary" : "ghost"}
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => changeState("open")}
              >
                Open
              </Button>
            </div>
          </div>

          <div
            className="flex-1 overflow-y-auto"
            onScroll={(e) => {
              const el = e.currentTarget;
              if (
                el.scrollHeight - el.scrollTop - el.clientHeight < 600 &&
                hasNextPage &&
                !isFetchingNextPage
              ) {
                fetchNextPage();
              }
            }}
          >
            {isLoading &&
              Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className="h-[124px] border-b px-4 py-3">
                  <Skeleton className="h-full w-full" />
                </div>
              ))}

            {!isLoading && items.length === 0 && (
              <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
                <Package className="h-12 w-12 opacity-40" />
                <p className="text-sm font-medium">Nothing bundled yet</p>
                <p className="max-w-[320px] text-sm text-muted-foreground">
                  Order mail appears here within a minute of arriving.
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

            {items.map((order) => (
              <OrderRow
                key={order.id}
                order={order}
                selected={order.id === selectedId}
                onSelect={() => selectOrder(order.id)}
              />
            ))}
          </div>
        </div>
      )}

      {(!isMobile || showDetailOnly) && (
        <div className="flex-1 overflow-y-auto">
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
              onDeleted={() => selectOrder(null)}
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
    </div>
  );
}
