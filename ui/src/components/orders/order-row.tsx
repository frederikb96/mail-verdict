import { useRef, useState } from "react";
import { format, isSameDay, isSameYear } from "date-fns";
import { CircleCheck, Lock, RotateCcw, Star } from "lucide-react";
import { ContextMenu, ContextMenuContent, ContextMenuTrigger } from "@/components/ui/context-menu";
import { OrderMenuItems } from "@/components/orders/order-actions";
import type { OrderActionId } from "@/lib/order-actions";
import { orderIconEntry } from "@/lib/order-icon";
import { swipeAction, swipeAxis, swipeOffset } from "@/lib/order-swipe";
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
  onAction: (order: OrderListItem, id: OrderActionId) => void;
}

/** A click that follows a swipe, or the long press that opened the menu,
 * is the same touch ending -- it must not also open the order. */
const CLICK_SUPPRESS_MS = 700;

interface Reveal {
  /** Which action the row is currently showing behind itself. */
  side: "favorite" | "close";
  /** Past the commit point: letting go now performs it. */
  armed: boolean;
}

interface Gesture {
  startX: number;
  startY: number;
  locked: boolean;
}

export function OrderRow({ order, selected, onSelect, onAction }: OrderRowProps) {
  const { Icon, tileClass } = orderIconEntry(order.icon);
  const contentRef = useRef<HTMLButtonElement>(null);
  const gestureRef = useRef<Gesture | null>(null);
  const revealRef = useRef<Reveal | null>(null);
  const menuOpenRef = useRef(false);
  const suppressClickUntilRef = useRef(0);
  const [reveal, setRevealState] = useState<Reveal | null>(null);

  // The render state only changes when the revealed side or the armed
  // flag does -- not per pointer move, which only writes the transform.
  const setReveal = (next: Reveal | null) => {
    const prev = revealRef.current;
    if (prev?.side === next?.side && prev?.armed === next?.armed) return;
    revealRef.current = next;
    setRevealState(next);
  };

  const settle = () => {
    const el = contentRef.current;
    if (el) {
      el.style.transition = "transform 150ms ease-out";
      el.style.transform = "";
    }
    gestureRef.current = null;
    setReveal(null);
  };

  // Touch only: a mouse (or pen) never swipes. `touch-action: pan-y` on
  // the wrapper leaves vertical panning to the browser -- which takes the
  // gesture over with a pointercancel -- and delivers a horizontal drag to
  // these handlers. Base UI's long press cancels itself once the finger
  // has moved more than 10 px (SWIPE_LOCK_PX is the same distance), so a
  // swipe and the menu's long press are exclusive by construction.
  const handlePointerDown = (e: React.PointerEvent) => {
    if (e.pointerType !== "touch" || menuOpenRef.current) return;
    const el = contentRef.current;
    if (el) el.style.transition = "none";
    gestureRef.current = { startX: e.clientX, startY: e.clientY, locked: false };
  };

  const handlePointerMove = (e: React.PointerEvent) => {
    const gesture = gestureRef.current;
    if (!gesture || e.pointerType !== "touch") return;
    if (menuOpenRef.current) {
      settle();
      return;
    }
    const dx = e.clientX - gesture.startX;
    const dy = e.clientY - gesture.startY;
    if (!gesture.locked) {
      const axis = swipeAxis(dx, dy);
      if (axis === "pending") return;
      if (axis === "vertical") {
        gestureRef.current = null;
        return;
      }
      gesture.locked = true;
    }
    const el = contentRef.current;
    if (el) el.style.transform = `translateX(${swipeOffset(dx)}px)`;
    const action = swipeAction(dx);
    setReveal({ side: dx < 0 ? "favorite" : "close", armed: action !== null });
  };

  const handlePointerUp = (e: React.PointerEvent) => {
    const gesture = gestureRef.current;
    if (!gesture || e.pointerType !== "touch") return;
    const wasSwipe = gesture.locked;
    const action = wasSwipe ? swipeAction(e.clientX - gesture.startX) : null;
    settle();
    if (wasSwipe) suppressClickUntilRef.current = performance.now() + CLICK_SUPPRESS_MS;
    if (action) onAction(order, action);
  };

  const handleClick = () => {
    if (performance.now() < suppressClickUntilRef.current) return;
    onSelect();
  };

  const swipeLabel =
    reveal?.side === "favorite"
      ? order.is_favorite
        ? "Unfavorite"
        : "Favorite"
      : order.is_open
        ? "Close"
        : "Reopen";

  return (
    <ContextMenu
      onOpenChange={(open) => {
        menuOpenRef.current = open;
        if (open) {
          // A long press that opens the menu ends with a touch release
          // that browsers may still deliver as a click.
          suppressClickUntilRef.current = performance.now() + CLICK_SUPPRESS_MS;
          if (gestureRef.current) settle();
        }
      }}
    >
      <ContextMenuTrigger className="relative h-[124px] touch-pan-y select-none overflow-hidden border-b">
        {reveal && (
          <div
            aria-hidden
            className={cn(
              "absolute inset-0 flex items-center px-5 transition-colors",
              reveal.side === "favorite"
                ? "justify-end bg-amber-500/15 text-amber-700 dark:text-amber-300"
                : "justify-start bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
              reveal.armed &&
                (reveal.side === "favorite" ? "bg-amber-500/30" : "bg-emerald-500/30"),
            )}
          >
            <span
              className={cn(
                "flex items-center gap-2 text-sm font-medium transition-transform",
                reveal.armed && "scale-110",
              )}
            >
              {reveal.side === "favorite" ? (
                <Star className="h-5 w-5" fill={order.is_favorite ? "none" : "currentColor"} />
              ) : order.is_open ? (
                <CircleCheck className="h-5 w-5" />
              ) : (
                <RotateCcw className="h-5 w-5" />
              )}
              {swipeLabel}
            </span>
          </div>
        )}
        <button
          ref={contentRef}
          type="button"
          onClick={handleClick}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={settle}
          data-testid="order-row"
          data-order-id={order.id}
          className={cn(
            "relative h-full w-full overflow-hidden px-4 py-3 flex gap-3 text-left bg-background hover:bg-accent/50",
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
              <span className="flex min-w-0 items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground leading-4">
                {order.is_favorite && (
                  <Star
                    className="h-3 w-3 shrink-0 text-amber-500"
                    fill="currentColor"
                    aria-label="Favorite"
                  />
                )}
                <span className="truncate">{order.merchant}</span>
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
              {order.is_sealed && (
                <Lock
                  className="h-3 w-3 text-muted-foreground"
                  aria-label="Sealed: no more mail is added"
                />
              )}
            </div>
            <p className="mt-1 text-xs leading-4 text-muted-foreground line-clamp-2">
              {order.summary_preview}
            </p>
          </div>
        </button>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <OrderMenuItems order={order} onAction={onAction} />
      </ContextMenuContent>
    </ContextMenu>
  );
}
