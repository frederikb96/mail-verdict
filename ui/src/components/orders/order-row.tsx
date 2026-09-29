import { format, isSameDay, isSameYear } from "date-fns";
import { orderIconEntry } from "@/lib/order-icon";
import { cn } from "@/lib/utils";
import type { OrderListItem } from "@/types/api";

/** "21 Apr – 23 Apr", one date when both fall on one day, year appended
 * when it isn't the current year -- the row's own compact date range. */
function formatDateRange(first: string | null, last: string | null): string {
  if (!first && !last) return "";
  const start = first ? new Date(first) : null;
  const end = last ? new Date(last) : null;
  const now = new Date();
  const fmt = (d: Date) => format(d, isSameYear(d, now) ? "d MMM" : "d MMM yyyy");
  if (start && end) {
    if (isSameDay(start, end)) return fmt(end);
    return `${fmt(start)} – ${fmt(end)}`;
  }
  return fmt((start ?? end) as Date);
}

interface OrderRowProps {
  order: OrderListItem;
  selected: boolean;
  onSelect: () => void;
}

export function OrderRow({ order, selected, onSelect }: OrderRowProps) {
  const { Icon, tileClass } = orderIconEntry(order.icon);
  return (
    <button
      type="button"
      onClick={onSelect}
      data-testid="order-row"
      data-order-id={order.id}
      className={cn(
        "h-[124px] w-full overflow-hidden border-b px-4 py-3 flex gap-3 text-left hover:bg-accent/50",
        selected && "bg-accent",
      )}
    >
      <div
        className={cn(
          "h-10 w-10 shrink-0 rounded-xl flex items-center justify-center",
          tileClass,
        )}
      >
        <Icon className="h-5 w-5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground truncate leading-4">
            {order.merchant}
          </span>
          <span className="text-xs text-muted-foreground tabular-nums shrink-0">
            {formatDateRange(order.first_mail_at, order.last_mail_at)}
          </span>
        </div>
        <div className="mt-0.5 text-sm font-semibold text-foreground truncate leading-5">
          {order.subject}
        </div>
        <div className="mt-1 flex items-center gap-2">
          <span
            className={cn(
              "inline-flex h-5 items-center rounded-full px-2 text-xs font-medium",
              order.is_open
                ? "bg-sky-500/10 text-sky-700 dark:text-sky-300"
                : "bg-muted text-muted-foreground",
            )}
          >
            {order.status || (order.is_open ? "open" : "finished")}
          </span>
          <span className="text-xs text-muted-foreground">
            · {order.mail_count} mail{order.mail_count === 1 ? "" : "s"}
          </span>
        </div>
        <p className="mt-1 text-xs leading-4 text-muted-foreground line-clamp-2">
          {order.summary_preview}
        </p>
      </div>
    </button>
  );
}
