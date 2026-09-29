"use client";

import { type RefObject, useLayoutEffect, useRef, useState } from "react";
import { format, isSameDay, isSameYear } from "date-fns";
import { CalendarDays, FileText, MoreHorizontal, Ticket as TicketIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { AttachmentPreviewDialog } from "@/components/mail/attachment-preview-dialog";
import { OrderPickerDialog } from "@/components/orders/order-picker-dialog";
import { OrderSummaryView } from "@/components/orders/order-summary-view";
import {
  useDeleteOrder,
  useDetachMail,
  useMergeOrder,
  useOrderDetail,
  useRewriteOrder,
} from "@/hooks/use-orders";
import { useOpenMessage } from "@/hooks/use-open-message";
import { useToast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import { formatRelativeDate, formatSize } from "@/lib/format";
import { clearOrderScrollAnchor, readOrderScrollAnchor } from "@/lib/order-scroll-anchor";
import { orderIconEntry } from "@/lib/order-icon";
import { cn } from "@/lib/utils";
import type { OrderDocument, OrderMail } from "@/types/api";

function formatDateRange(first: string | null, last: string | null): string {
  if (!first && !last) return "";
  const start = first ? new Date(first) : null;
  const end = last ? new Date(last) : null;
  const now = new Date();
  const fmt = (d: Date) => format(d, isSameYear(d, now) ? "d MMM" : "d MMM yyyy");
  if (start && end) return isSameDay(start, end) ? fmt(end) : `${fmt(start)} – ${fmt(end)}`;
  return fmt((start ?? end) as Date);
}

const KIND_LABELS: Record<string, string> = {
  order_number: "Order",
  booking_code: "Booking",
  tracking_number: "Tracking",
  invoice_number: "Invoice",
  ticket_number: "Ticket",
};

const DOCUMENT_ICONS: Record<string, typeof FileText> = {
  "application/pdf": FileText,
  "application/vnd.apple.pkpass": TicketIcon,
  "text/calendar": CalendarDays,
  "application/ics": CalendarDays,
};

interface OrderDetailProps {
  orderId: string;
  onBack?: () => void;
  onDeleted: () => void;
  /** The pane's own scroll container -- one level up in orders-page.tsx,
   * since it also hosts the phone-width back bar above this component. */
  scrollContainerRef?: RefObject<HTMLDivElement | null>;
  /** Called just before a mail row hands off to the normal mail view, so
   * the caller can remember where the list itself was sitting too. */
  onBeforeOpenMail?: (mailKey: string, rowTop: number) => void;
}

export function OrderDetailPane({
  orderId,
  onBack,
  onDeleted,
  scrollContainerRef,
  onBeforeOpenMail,
}: OrderDetailProps) {
  const { data: order, isLoading } = useOrderDetail(orderId);
  const { openMessageById } = useOpenMessage();
  const { push: pushToast } = useToast();
  const deleteOrder = useDeleteOrder();
  const rewriteOrder = useRewriteOrder();
  const mergeOrder = useMergeOrder();
  const detachMail = useDetachMail();

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmMerge, setConfirmMerge] = useState<string | null>(null);
  const [pickingMerge, setPickingMerge] = useState(false);
  const [movingMail, setMovingMail] = useState<OrderMail | null>(null);
  const [previewDoc, setPreviewDoc] = useState<OrderDocument | null>(null);

  const mailRowRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  // Which anchor ("orderId:mailKey") has already been applied -- not
  // which order.id has been "seen": on a Back navigation the browser can
  // restore this same mounted component rather than remounting it, so a
  // guard keyed on order.id alone would have already marked this order
  // "attempted" on the very first visit, before any anchor existed, and
  // then silently skip the real one written moments later by opening a
  // mail. Keying on the anchor's own identity means a fresh anchor is
  // never mistaken for one already handled.
  const appliedAnchorKeyRef = useRef<string | null>(null);

  // Coming back to this order after opening one of its mails: put the mail
  // row back exactly where it sat on screen. Held while the surrounding
  // content settles (documents/summary can still be loading), released on
  // the reader's first gesture or after 1.5s -- see the scrolling notes on
  // never leaving a hold running forever.
  //
  // A Back navigation is not guaranteed to change `order` or
  // `scrollContainerRef`'s own identity -- the router can restore this
  // same mounted component rather than remounting it, and React then
  // never re-runs an effect whose dependencies read as unchanged, even
  // though the anchor a click just wrote (external to React state) is
  // new. popstate/pageshow/visibilitychange are the ordinary signals a
  // Back navigation fires regardless of whether React itself re-renders,
  // so the check also runs from those, not only from the dependency
  // array.
  useLayoutEffect(() => {
    // The active hold's own release, if one is running -- so the outer
    // effect's cleanup (an actual unmount, or order/scrollContainerRef
    // genuinely changing) can tear it down too, not only its own natural
    // end. tryApply can run more than once (mount, then again on a later
    // popstate); without this, an earlier call's listeners would outlive
    // whatever tore this effect down.
    let currentRelease: (() => void) | null = null;

    const tryApply = () => {
      const container = scrollContainerRef?.current;
      if (!order || !container) return;
      const anchor = readOrderScrollAnchor(order.id);
      if (!anchor) return;
      const anchorKey = `${anchor.orderId}:${anchor.mailKey}`;
      if (appliedAnchorKeyRef.current === anchorKey) return;
      const rowEl = mailRowRefs.current.get(anchor.mailKey);
      if (!rowEl) return; // Mail rows not painted yet -- retry once they are.

      appliedAnchorKeyRef.current = anchorKey;

      const apply = () => {
        const containerRect = container.getBoundingClientRect();
        const rowRect = rowEl.getBoundingClientRect();
        container.scrollTop += rowRect.top - containerRect.top - anchor.rowTop;
      };
      apply();

      let released = false;
      const release = () => {
        if (released) return;
        released = true;
        resizeObserver.disconnect();
        container.removeEventListener("wheel", release);
        container.removeEventListener("touchstart", release);
        container.removeEventListener("pointerdown", release);
        container.removeEventListener("keydown", release);
        window.clearTimeout(timeoutId);
        clearOrderScrollAnchor();
        if (currentRelease === release) currentRelease = null;
      };
      currentRelease = release;

      const resizeObserver = new ResizeObserver(() => {
        if (!released) apply();
      });
      resizeObserver.observe(container);

      container.addEventListener("wheel", release, { passive: true });
      container.addEventListener("touchstart", release, { passive: true });
      container.addEventListener("pointerdown", release);
      container.addEventListener("keydown", release);
      const timeoutId = window.setTimeout(release, 1500);
    };

    tryApply();
    window.addEventListener("popstate", tryApply);
    window.addEventListener("pageshow", tryApply);
    document.addEventListener("visibilitychange", tryApply);
    return () => {
      window.removeEventListener("popstate", tryApply);
      window.removeEventListener("pageshow", tryApply);
      document.removeEventListener("visibilitychange", tryApply);
      currentRelease?.();
    };
  }, [order, scrollContainerRef]);

  if (isLoading || !order) {
    return (
      <div className="mx-auto max-w-[720px] px-6 py-6 flex flex-col gap-6">
        <div className="flex items-start gap-4">
          <Skeleton className="h-12 w-12 rounded-2xl" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-6 w-2/3" />
          </div>
        </div>
        <Skeleton className="h-40 w-full rounded-xl" />
      </div>
    );
  }

  const { Icon, tileClass } = orderIconEntry(order.icon);

  const handleOpenMail = async (mail: OrderMail) => {
    if (mail.location !== "mailbox" || !mail.message_id) return;
    const rowEl = mailRowRefs.current.get(mail.key);
    const container = scrollContainerRef?.current;
    const rowTop =
      rowEl && container
        ? rowEl.getBoundingClientRect().top - container.getBoundingClientRect().top
        : 0;
    onBeforeOpenMail?.(mail.key, rowTop);
    await openMessageById(mail.message_id);
  };

  return (
    <div className="mx-auto max-w-[720px] px-6 py-6 flex flex-col gap-6">
      {onBack && (
        <Button variant="ghost" size="sm" className="w-fit" onClick={onBack}>
          Back
        </Button>
      )}
      <div className="flex items-start gap-4">
        <div className={cn("h-12 w-12 rounded-2xl flex items-center justify-center", tileClass)}>
          <Icon className="h-6 w-6" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {order.merchant}
          </div>
          <h1 className="text-xl font-semibold leading-7 line-clamp-3">{order.subject}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span
              className={cn(
                "inline-flex h-6 items-center rounded-full px-2.5 text-sm font-medium",
                order.is_open
                  ? "bg-sky-500/10 text-sky-700 dark:text-sky-300"
                  : "bg-muted text-muted-foreground",
              )}
            >
              {order.status || (order.is_open ? "open" : "finished")}
            </span>
            <span className="text-xs text-muted-foreground">
              {order.mail_count} mail{order.mail_count === 1 ? "" : "s"} ·{" "}
              {formatDateRange(order.first_mail_at, order.last_mail_at)}
            </span>
          </div>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="ghost" size="icon" aria-label="Order actions" />}>
            <MoreHorizontal className="h-4 w-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={() => rewriteOrder.mutate(order.id)}>
              Rewrite summary
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setPickingMerge(true)}>
              Merge into another order…
            </DropdownMenuItem>
            <DropdownMenuItem
              className="text-destructive"
              onClick={() => setConfirmDelete(true)}
            >
              Delete order…
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className="rounded-xl border bg-card p-4 text-sm leading-6">
        <OrderSummaryView text={order.summary} />
        {order.text_stale && (
          <p className="mt-2 text-xs text-muted-foreground">Updating the summary…</p>
        )}
      </div>

      {order.identifiers.length > 0 && (
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mb-2">
            Numbers
          </div>
          <div className="flex flex-wrap gap-2">
            {order.identifiers.map((identifier, i) => (
              <button
                key={i}
                type="button"
                aria-label={`Copy ${KIND_LABELS[identifier.kind] ?? identifier.kind} number ${identifier.value}`}
                className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs"
                onClick={() => {
                  navigator.clipboard.writeText(identifier.value);
                  pushToast("Copied", "success");
                }}
              >
                <span className="text-muted-foreground">
                  {KIND_LABELS[identifier.kind] ?? identifier.kind}
                </span>
                <span className="font-mono">{identifier.value}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {order.documents.length > 0 && (
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mb-2">
            Documents
          </div>
          <div className="flex flex-wrap gap-2">
            {order.documents.map((doc) => {
              const DocIcon = DOCUMENT_ICONS[doc.content_type] ?? FileText;
              const isPdf = doc.content_type === "application/pdf";
              return (
                <button
                  key={doc.attachment_id}
                  type="button"
                  className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs"
                  onClick={() => {
                    if (isPdf) {
                      setPreviewDoc(doc);
                    } else {
                      window.open(
                        api.mails.attachmentUrl(doc.message_id, doc.attachment_id),
                        "_blank",
                      );
                    }
                  }}
                >
                  <DocIcon className="h-3.5 w-3.5" />
                  <span>{doc.filename.slice(0, 28)}</span>
                  <span className="text-muted-foreground">{formatSize(doc.size_bytes)}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      <div>
        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mb-2">
          Mails
        </div>
        <div className="rounded-xl border overflow-hidden">
          {order.mails.map((mail) => (
            <div
              key={mail.key}
              ref={(el) => {
                if (el) mailRowRefs.current.set(mail.key, el);
                else mailRowRefs.current.delete(mail.key);
              }}
              data-testid="order-mail-row"
              data-mail-key={mail.key}
              className={cn(
                "h-14 px-4 flex items-center gap-3 border-b last:border-b-0",
                mail.location === "gone" && "opacity-50",
              )}
              title={mail.location === "gone" ? "No longer in the mailbox" : undefined}
            >
              <button
                type="button"
                disabled={mail.location === "gone"}
                onClick={() => handleOpenMail(mail)}
                className="min-w-0 flex-1 text-left disabled:cursor-default"
              >
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "h-2 w-2 rounded-full shrink-0",
                      mail.is_seen === false
                        ? "bg-sky-500"
                        : "border border-muted-foreground/40",
                    )}
                  />
                  <span className="text-sm font-medium truncate">{mail.from_addr}</span>
                  <span className="ml-auto text-xs text-muted-foreground tabular-nums shrink-0">
                    {formatRelativeDate(mail.received_at)}
                  </span>
                </div>
                <div className="text-sm text-muted-foreground truncate">{mail.subject}</div>
              </button>
              {mail.location !== "gone" && (
                <DropdownMenu>
                  <DropdownMenuTrigger
                    render={
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Actions for ${mail.subject}`}
                      />
                    }
                  >
                    <MoreHorizontal className="h-4 w-4" />
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem
                      onClick={() => detachMail.mutate({ orderId: order.id, mailKey: mail.key })}
                    >
                      Remove from this order
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => setMovingMail(mail)}>
                      Move to another order…
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
          ))}
        </div>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete this order?"
        description={`Its ${order.mail_count} mail${order.mail_count === 1 ? "" : "s"} stay where they are.`}
        confirmLabel="Delete order"
        onConfirm={() => {
          setConfirmDelete(false);
          deleteOrder.mutate(order.id);
          onDeleted();
        }}
      />

      <OrderPickerDialog
        open={pickingMerge}
        onOpenChange={setPickingMerge}
        excludeId={order.id}
        onChoose={(targetId) => setConfirmMerge(targetId)}
      />
      <ConfirmDialog
        open={confirmMerge !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmMerge(null);
        }}
        title="Merge this order into the chosen one?"
        description={`Its ${order.mail_count} mail${order.mail_count === 1 ? "" : "s"} move over and this order disappears.`}
        confirmLabel="Merge"
        onConfirm={() => {
          const into = confirmMerge;
          setConfirmMerge(null);
          if (into) {
            mergeOrder.mutate({ id: order.id, into });
            onDeleted();
          }
        }}
      />

      <OrderPickerDialog
        open={movingMail !== null}
        onOpenChange={(open) => {
          if (!open) setMovingMail(null);
        }}
        excludeId={order.id}
        onChoose={(targetId) => {
          if (movingMail) {
            detachMail.mutate({ orderId: order.id, mailKey: movingMail.key, moveTo: targetId });
          }
          setMovingMail(null);
        }}
      />

      {previewDoc && (
        <AttachmentPreviewDialog
          messageId={previewDoc.message_id}
          attachment={{
            id: previewDoc.attachment_id,
            filename: previewDoc.filename,
            content_type: previewDoc.content_type,
            size_bytes: previewDoc.size_bytes,
          }}
          onOpenChange={(open) => {
            if (!open) setPreviewDoc(null);
          }}
        />
      )}
    </div>
  );
}
