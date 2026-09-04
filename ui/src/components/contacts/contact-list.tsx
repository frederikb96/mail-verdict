"use client";

/** The 360px list half of the contacts page: search, address-book filter
 * chips, and a VList of uniform rows -- the mail list's own pattern, since
 * contact rows are uniform height. */

import { useCallback, useMemo, useRef, useState } from "react";
import { useAtom } from "jotai";
import { VList, type VListHandle } from "virtua";
import { Loader2, Search, UserRound } from "lucide-react";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useAddressbooks, useContacts } from "@/hooks/use-contacts";
import { selectedContactIdAtom } from "@/lib/atoms";
import { cn } from "@/lib/utils";
import type { Contact } from "@/types/api";

type Row = { kind: "letter"; letter: string } | { kind: "contact"; contact: Contact };

function letterFor(contact: Contact): string {
  const c = contact.summary.trim().charAt(0).toUpperCase();
  return /[A-Z]/.test(c) ? c : "#";
}

export function ContactList() {
  const [selectedId, setSelectedId] = useAtom(selectedContactIdAtom);
  const [query, setQuery] = useState("");
  const [addressbookId, setAddressbookId] = useState<string | undefined>(undefined);
  const vlistRef = useRef<VListHandle>(null);

  const { data: addressbooks } = useAddressbooks();
  const { data, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage } = useContacts(
    addressbookId,
    query,
  );

  const contacts = useMemo(() => data?.pages.flatMap((p) => p.contacts) ?? [], [data]);

  const rows = useMemo<Row[]>(() => {
    const sorted = [...contacts].sort((a, b) => a.summary.localeCompare(b.summary));
    const result: Row[] = [];
    let lastLetter: string | null = null;
    for (const contact of sorted) {
      const letter = letterFor(contact);
      if (letter !== lastLetter) {
        result.push({ kind: "letter", letter });
        lastLetter = letter;
      }
      result.push({ kind: "contact", contact });
    }
    return result;
  }, [contacts]);

  const handleScroll = useCallback(
    (offset: number) => {
      if (!vlistRef.current) return;
      const { scrollSize, viewportSize } = vlistRef.current;
      if (scrollSize - offset - viewportSize < 200 && hasNextPage && !isFetchingNextPage) {
        fetchNextPage();
      }
    },
    [hasNextPage, isFetchingNextPage, fetchNextPage],
  );

  return (
    <div className="flex h-full w-full flex-col">
      <div className="flex flex-col gap-2 border-b p-2">
        <div className="relative">
          <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search contacts"
            className="pl-7"
          />
        </div>
        {addressbooks && addressbooks.length > 1 && (
          <div className="flex flex-wrap gap-1">
            <button
              type="button"
              onClick={() => setAddressbookId(undefined)}
              className={cn(
                "rounded-full border px-2 py-0.5 text-xs",
                !addressbookId && "border-primary bg-primary/10",
              )}
            >
              All
            </button>
            {addressbooks.map((ab) => (
              <button
                key={ab.id}
                type="button"
                onClick={() => setAddressbookId(ab.id)}
                className={cn(
                  "flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs",
                  addressbookId === ab.id && "border-primary bg-primary/10",
                )}
              >
                {ab.display_name}
                {ab.read_only && <Badge variant="outline" className="h-3.5 px-1 text-[9px]">RO</Badge>}
              </button>
            ))}
          </div>
        )}
      </div>

      {isLoading ? (
        <div className="flex flex-1 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : rows.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-muted-foreground">
          <UserRound className="h-10 w-10 opacity-50" />
          <p className="text-sm">No contacts found</p>
        </div>
      ) : (
        <VList ref={vlistRef} className="flex-1" style={{ height: "100%" }} onScroll={handleScroll}>
          {rows.map((row) =>
            row.kind === "letter" ? (
              <div
                key={`letter-${row.letter}`}
                style={{ height: 24 }}
                className="flex items-center bg-muted/40 px-3 text-xs font-medium text-muted-foreground"
              >
                {row.letter}
              </div>
            ) : (
              <button
                key={row.contact.id}
                type="button"
                onClick={() => setSelectedId(row.contact.id)}
                style={{ height: 56 }}
                className={cn(
                  "flex w-full items-center gap-3 border-b px-3 text-left hover:bg-accent/50",
                  selectedId === row.contact.id && "bg-accent",
                )}
              >
                <InitialsAvatar name={row.contact.summary} />
                <div className="flex min-w-0 flex-col">
                  <span className="truncate text-sm font-medium">{row.contact.summary}</span>
                  <span className="truncate text-xs text-muted-foreground">
                    {row.contact.emails[0]?.email ?? ""}
                  </span>
                </div>
                {row.contact.read_only && (
                  <Badge variant="outline" className="ml-auto text-[9px]">
                    RO
                  </Badge>
                )}
              </button>
            ),
          )}
        </VList>
      )}
      {isFetchingNextPage && (
        <div className="flex items-center justify-center py-2">
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        </div>
      )}
    </div>
  );
}
