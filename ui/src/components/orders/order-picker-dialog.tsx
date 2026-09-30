"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { orderIconEntry } from "@/lib/order-icon";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

interface OrderPickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Excluded from the results -- the order this picker is choosing a
   * target away from. */
  excludeId?: string;
  onChoose: (orderId: string) => void;
}

/** A text field that filters the newest 100 orders by merchant and
 * subject in the browser -- the target picker for merge and move. */
export function OrderPickerDialog({
  open,
  onOpenChange,
  excludeId,
  onChoose,
}: OrderPickerDialogProps) {
  const [query, setQuery] = useState("");
  const { data } = useQuery({
    queryKey: ["orders", "picker"],
    queryFn: () => api.orders.list({ state: "all", limit: 100 }),
    enabled: open,
  });

  const items = useMemo(() => {
    const all = (data?.items ?? []).filter((o) => o.id !== excludeId);
    const q = query.trim().toLowerCase();
    if (!q) return all;
    return all.filter(
      (o) => o.merchant.toLowerCase().includes(q) || o.subject.toLowerCase().includes(q),
    );
  }, [data, query, excludeId]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Choose an order</DialogTitle>
        </DialogHeader>
        <Input
          autoFocus
          placeholder="Search by merchant or subject…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="max-h-80 overflow-y-auto -mx-1">
          {items.map((order) => {
            const { Icon, tileClass } = orderIconEntry(order.icon);
            return (
              <button
                key={order.id}
                type="button"
                onClick={() => {
                  onChoose(order.id);
                  onOpenChange(false);
                }}
                className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-accent"
              >
                <div
                  className={cn(
                    "h-8 w-8 shrink-0 rounded-lg flex items-center justify-center",
                    tileClass,
                  )}
                >
                  <Icon className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium text-muted-foreground uppercase">
                    {order.merchant}
                  </div>
                  <div className="truncate text-sm">{order.subject}</div>
                </div>
              </button>
            );
          })}
          {items.length === 0 && (
            <p className="p-3 text-sm text-muted-foreground">No matching orders</p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
