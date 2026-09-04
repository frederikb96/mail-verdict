"use client";

/** The 360px list half of the contacts page: search, address-book filter
 * chips, and a VList of uniform rows -- the mail list's own pattern, since
 * contact rows are uniform height. Rows support the same checkbox-on-hover
 * multi-selection gesture the mail list uses (see mail-list-item.tsx):
 * click the checkbox to toggle, shift-click to select a range, plain click
 * on the row opens it and clears any multi-selection. */

import { useCallback, useMemo, useRef, useState } from "react";
import { VList, type VListHandle } from "virtua";
import { Loader2, Search, UserRound, X } from "lucide-react";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { useAddressbooks, useContacts, useContactSelection, useDeleteContact } from "@/hooks/use-contacts";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import type { Contact } from "@/types/api";

type Row = { kind: "letter"; letter: string } | { kind: "contact"; contact: Contact };

function letterFor(contact: Contact): string {
  const c = contact.summary.trim().charAt(0).toUpperCase();
  return /[A-Z]/.test(c) ? c : "#";
}

export function ContactList() {
  const { selectedId, selectContact } = useContactSelection();
  const [query, setQuery] = useState("");
  const [addressbookId, setAddressbookId] = useState<string | undefined>(undefined);
  const vlistRef = useRef<VListHandle>(null);

  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [anchorId, setAnchorId] = useState<string | null>(null);
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const deleteContact = useDeleteContact();
  const { push: pushToast } = useToast();

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

  const contactIdsInOrder = useMemo(
    () => rows.filter((r) => r.kind === "contact").map((r) => (r as { contact: Contact }).contact.id),
    [rows],
  );

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

  const handleCheckToggle = useCallback(
    (contactId: string, shiftKey: boolean) => {
      setCheckedIds((prev) => {
        const next = new Set(prev);
        if (shiftKey && anchorId) {
          const from = contactIdsInOrder.indexOf(anchorId);
          const to = contactIdsInOrder.indexOf(contactId);
          if (from !== -1 && to !== -1) {
            const [start, end] = from < to ? [from, to] : [to, from];
            for (let i = start; i <= end; i++) next.add(contactIdsInOrder[i]);
          }
        } else if (next.has(contactId)) {
          next.delete(contactId);
        } else {
          next.add(contactId);
        }
        return next;
      });
      setAnchorId(contactId);
    },
    [anchorId, contactIdsInOrder],
  );

  const handleRowClick = useCallback(
    (contactId: string) => {
      if (checkedIds.size > 0) {
        setCheckedIds(new Set());
        setAnchorId(null);
      }
      selectContact(contactId);
    },
    [checkedIds, selectContact],
  );

  const clearSelection = useCallback(() => {
    setCheckedIds(new Set());
    setAnchorId(null);
  }, []);

  const handleBulkDelete = useCallback(() => {
    const ids = Array.from(checkedIds);
    Promise.allSettled(ids.map((id) => deleteContact.mutateAsync(id))).then((results) => {
      const failed = results.filter((r) => r.status === "rejected").length;
      if (failed > 0) {
        pushToast(`Deleted ${ids.length - failed} of ${ids.length} contacts`, "warning");
      }
      if (selectedId && ids.includes(selectedId)) selectContact(null);
      setConfirmBulkDelete(false);
      clearSelection();
    });
  }, [checkedIds, deleteContact, pushToast, selectedId, selectContact, clearSelection]);

  return (
    <div className="flex h-full w-full flex-col" data-slot="contact-list">
      {checkedIds.size > 0 ? (
        <div className="flex items-center gap-2 border-b p-2">
          <Button variant="ghost" size="icon-sm" onClick={clearSelection} aria-label="Clear selection">
            <X className="h-4 w-4" />
          </Button>
          <span className="text-sm font-medium">{checkedIds.size} selected</span>
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto text-destructive"
            onClick={() => setConfirmBulkDelete(true)}
          >
            Delete
          </Button>
        </div>
      ) : (
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
      )}

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
              <div
                key={row.contact.id}
                style={{ height: 56 }}
                className={cn(
                  "group flex w-full cursor-pointer items-center gap-3 border-b px-3 hover:bg-accent/50",
                  selectedId === row.contact.id && "bg-accent",
                  checkedIds.has(row.contact.id) && "bg-accent/70",
                )}
                onClick={() => handleRowClick(row.contact.id)}
              >
                {/* Checkbox (visible once any row is checked, or on hover --
                    the mail list's own pattern, mail-list-item.tsx) */}
                <div
                  className={cn(
                    "h-8 w-8 shrink-0 items-center justify-center",
                    checkedIds.size > 0 ? "flex" : "hidden group-hover:flex",
                  )}
                >
                  <Checkbox
                    checked={checkedIds.has(row.contact.id)}
                    onCheckedChange={() => {}}
                    onClick={(e) => {
                      e.stopPropagation();
                      handleCheckToggle(row.contact.id, e.shiftKey);
                    }}
                    aria-label={
                      checkedIds.has(row.contact.id)
                        ? `Deselect ${row.contact.summary}`
                        : `Select ${row.contact.summary}`
                    }
                  />
                </div>
                <InitialsAvatar
                  name={row.contact.summary}
                  photoUrl={row.contact.photo?.kind === "embedded" ? row.contact.photo.url : null}
                  className={cn(
                    "shrink-0",
                    checkedIds.size > 0 && "hidden",
                    checkedIds.size === 0 && "group-hover:hidden",
                  )}
                />
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
              </div>
            ),
          )}
        </VList>
      )}
      {isFetchingNextPage && (
        <div className="flex items-center justify-center py-2">
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        </div>
      )}

      <ConfirmDialog
        open={confirmBulkDelete}
        onOpenChange={setConfirmBulkDelete}
        title={`Delete ${checkedIds.size} contact${checkedIds.size === 1 ? "" : "s"}?`}
        description="This removes them from their address books. It cannot be undone."
        isConfirming={deleteContact.isPending}
        onConfirm={handleBulkDelete}
      />
    </div>
  );
}
