"use client";

import { useState } from "react";
import { Copy, ExternalLink } from "lucide-react";
import { OrderCodeDialog } from "@/components/orders/order-code-dialog";
import { useToast } from "@/hooks/use-toast";
import { factHref, factValueText, splitFacts } from "@/lib/order-facts";
import { cn } from "@/lib/utils";
import type { OrderFact, OrderFactKind } from "@/types/api";

/** Shown when the model gave a fact no label of its own. */
const DEFAULT_LABELS: Record<OrderFactKind, string> = {
  order_number: "Order number",
  booking_code: "Booking code",
  ticket_number: "Ticket number",
  invoice_number: "Invoice number",
  tracking_number: "Tracking",
  date: "Date",
  time: "Time",
  amount: "Amount",
  place: "Place",
  seat: "Seat",
  person: "Person",
  code: "Code",
  link: "Link",
  other: "Detail",
};

/** Numbers someone reads out or pastes into a form: set in monospace. */
const MONO_KINDS = new Set<OrderFactKind>([
  "order_number",
  "booking_code",
  "ticket_number",
  "invoice_number",
  "tracking_number",
]);

function Heading({ children }: { children: string }) {
  return (
    <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </div>
  );
}

/** The order's typed facts: codes as tappable tiles, everything else as
 * label / value rows. Tapping a row copies its value; a tracking number or
 * a link opens in a new tab and gets a copy button of its own. */
export function OrderFacts({ facts }: { facts: OrderFact[] }) {
  const { push: pushToast } = useToast();
  const [openCode, setOpenCode] = useState<OrderFact | null>(null);
  const { codes, rows } = splitFacts(facts);
  const now = new Date();

  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      pushToast("Copied", "success", 2000);
    } catch {
      pushToast("Could not copy", "error");
    }
  };

  if (codes.length === 0 && rows.length === 0) return null;

  return (
    <section aria-label="Key facts">
      <Heading>Key facts</Heading>

      {codes.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-3">
          {codes.map((fact) => (
            <button
              key={fact.id}
              type="button"
              data-testid="order-code-tile"
              aria-label={`Show ${fact.label} full screen`}
              onClick={() => setOpenCode(fact)}
              className="flex w-36 flex-col items-center gap-1.5 rounded-xl border bg-card p-2 text-center outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              <span className="flex aspect-square w-full items-center justify-center rounded-lg bg-white p-2">
                {/* eslint-disable-next-line @next/next/no-img-element -- a generated PNG */}
                <img
                  src={`${fact.code?.image_url}?scale=8`}
                  alt=""
                  draggable={false}
                  loading="lazy"
                  className="max-h-full max-w-full object-contain"
                />
              </span>
              <span className="w-full truncate text-xs font-medium">{fact.label}</span>
            </button>
          ))}
        </div>
      )}

      {rows.length > 0 && (
        <div className="overflow-hidden rounded-xl border bg-card">
          {rows.map((fact) => {
            const label = fact.label || DEFAULT_LABELS[fact.kind];
            const value = factValueText(fact, now);
            const href = factHref(fact);
            const valueClass = cn(
              "min-w-0 flex-1 break-words text-sm",
              MONO_KINDS.has(fact.kind) && "font-mono",
            );
            return (
              <div
                key={fact.id}
                data-testid="order-fact-row"
                className="flex min-h-11 items-stretch border-b last:border-b-0"
              >
                {href ? (
                  <>
                    <div className="flex min-w-0 flex-1 items-center gap-3 px-4 py-2">
                      <span className="w-28 shrink-0 text-xs text-muted-foreground sm:w-32">
                        {label}
                      </span>
                      <a
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        className={cn(valueClass, "inline-flex items-center gap-1.5 text-primary underline-offset-2 hover:underline")}
                      >
                        <span className="min-w-0 break-all">{value}</span>
                        <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />
                      </a>
                    </div>
                    <button
                      type="button"
                      aria-label={`Copy ${label}`}
                      onClick={() => copy(fact.value)}
                      className="flex w-11 shrink-0 items-center justify-center text-muted-foreground hover:bg-accent/50 hover:text-foreground"
                    >
                      <Copy className="h-4 w-4" />
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    aria-label={`Copy ${label}: ${value}`}
                    onClick={() => copy(fact.value)}
                    className="flex min-w-0 flex-1 items-center gap-3 px-4 py-2 text-left hover:bg-accent/50"
                  >
                    <span className="w-28 shrink-0 text-xs text-muted-foreground sm:w-32">
                      {label}
                    </span>
                    <span className={valueClass}>{value}</span>
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      <OrderCodeDialog
        fact={openCode}
        onOpenChange={(open) => {
          if (!open) setOpenCode(null);
        }}
      />
    </section>
  );
}
