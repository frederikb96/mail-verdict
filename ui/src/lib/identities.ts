/** Matching a message's addresses against an account's sending identities. */

import { extractEmail } from "@/lib/format";
import type { Identity } from "@/types/api";

/**
 * Which of an account's identities, if any, one of the given addresses
 * names -- case-insensitive, since header casing means nothing.
 *
 * An identity IS the alias: each address an account may send as has its
 * own Identity row, so matching a message's raw To/Cc headers against the
 * full identity list already covers the address-arrived-at-an-alias case
 * without any separate alias table to consult. Addresses are checked in
 * the order given, so a caller passing [to_addrs, cc_addrs] prefers a
 * direct match over one found only in Cc.
 */
export function matchIdentity(
  addresses: (string | null | undefined)[],
  identities: Identity[] | undefined,
): string | undefined {
  if (!identities || identities.length === 0) return undefined;
  const byAddress = new Map(identities.map((i) => [i.address.toLowerCase(), i.id]));
  for (const addr of addresses) {
    if (!addr) continue;
    const match = byAddress.get(extractEmail(addr).toLowerCase());
    if (match) return match;
  }
  return undefined;
}
