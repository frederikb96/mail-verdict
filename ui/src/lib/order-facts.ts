/** What the Key facts section shows for each fact: the text, where it
 * links, which ones are barcodes. Pure; see order-facts.test.ts. */

import { formatCalendarDate } from "@/lib/order-dates";
import type { OrderFact } from "@/types/api";

/** Only web links are ever followed -- a fact's text comes from a mail. */
export function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

/** Where tapping the fact's value goes, or null when it only copies. */
export function factHref(fact: OrderFact): string | null {
  if (fact.kind === "tracking_number") {
    return fact.tracking_url && isHttpUrl(fact.tracking_url) ? fact.tracking_url : null;
  }
  if (fact.kind === "link") return isHttpUrl(fact.value) ? fact.value : null;
  return null;
}

/** The value as shown: a plain date is written out, everything else is the
 * source's own wording. */
export function factValueText(fact: OrderFact, now: Date): string {
  return fact.kind === "date" ? formatCalendarDate(fact.value, now) : fact.value;
}

/** Barcodes with something to draw come apart from the key-value rows. */
export function splitFacts(facts: OrderFact[]): { codes: OrderFact[]; rows: OrderFact[] } {
  const codes: OrderFact[] = [];
  const rows: OrderFact[] = [];
  for (const fact of facts) {
    if (fact.kind === "code" && fact.code !== null) codes.push(fact);
    else rows.push(fact);
  }
  return { codes, rows };
}
