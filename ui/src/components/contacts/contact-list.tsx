"use client";

/** The 360px list half of the contacts page: search, address-book filter
 * chips, and a VList of uniform rows -- the mail list's own pattern, since
 * contact rows are uniform height. Rows support the same checkbox-on-hover
 * multi-selection gesture the mail list uses (see mail-list-item.tsx):
 * click the checkbox to toggle, shift-click to select a range, plain click
 * on the row opens it and clears any multi-selection. */

import { useCallback, useMemo, useRef, useState } from "react";
import { VList, type VListHandle } from "virtua";
import { Loader2, Lock, Search, UserRound, X } from "lucide-react";
import { InitialsAvatar } from "@/components/common/initials-avatar";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  useAddressbooks,
  useContactGroups,
  useContacts,
  useContactSelection,
  useDeleteContact,
} from "@/hooks/use-contacts";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import type { Contact } from "@/types/api";

// Select requires a non-empty item value, so "every address book" / "every
// group" needs its own sentinel rather than the natural `undefined` --
// same convention search-page.tsx uses for "All accounts".
const ALL_ADDRESSBOOKS_VALUE = "__all__";
const ALL_GROUPS_VALUE = "__all__";

type Row = { kind: "letter"; letter: string } | { kind: "contact"; contact: Contact };

function letterFor(contact: Contact): string {
  // Diacritics are stripped before bucketing because the list is sorted with
  // localeCompare, which files "Anders" and "Änne" together: bucketing the
  // accented one under "#" would drop a second "#" header into the middle of
  // the A's, and another one at every accented name further down.
  const c = contact.summary
    .trim()
    .charAt(0)
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toUpperCase();
  return /[A-Z]/.test(c) ? c : "#";
}

export function ContactList() {
  const { selectedId, selectContact } = useContactSelection();
  const [query, setQuery] = useState("");
  const [addressbookId, setAddressbookId] = useState<string | undefined>(undefined);
  const [groupId, setGroupId] = useState<string | undefined>(undefined);
  const vlistRef = useRef<VListHandle>(null);

  // A group belongs to the address book it was scanned from (a group card
  // always, a category almost always in practice); switching books makes
  // whatever was selected no longer meaningful, so it is cleared rather
  // than silently carried over into a book that has never heard of it.
  const handleAddressbookChange = useCallback((id: string | undefined) => {
    setAddressbookId(id);
    setGroupId(undefined);
  }, []);

  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [anchorId, setAnchorId] = useState<string | null>(null);
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const deleteContact = useDeleteContact();
  const { push: pushToast } = useToast();

  const { data: addressbooks } = useAddressbooks();
  const { data: groupsData } = useContactGroups(addressbookId);
  const groups = groupsData?.groups ?? [];
  const { data, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage } = useContacts(
    addressbookId,
    query,
    groupId,
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
          {(addressbooks && addressbooks.length > 1) || groups.length > 0 ? (
            <div className="flex gap-1.5">
              {addressbooks && addressbooks.length > 1 && (
                <Select
                  value={addressbookId ?? ALL_ADDRESSBOOKS_VALUE}
                  onValueChange={(v) =>
                    handleAddressbookChange(!v || v === ALL_ADDRESSBOOKS_VALUE ? undefined : v)
                  }
                >
                  <SelectTrigger size="sm" className="min-w-0 flex-1">
                    <SelectValue placeholder="All address books">
                      {(v: string) =>
                        v === ALL_ADDRESSBOOKS_VALUE
                          ? "All address books"
                          : (addressbooks.find((ab) => ab.id === v)?.display_name ??
                            "All address books")
                      }
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL_ADDRESSBOOKS_VALUE}>All address books</SelectItem>
                    {addressbooks.map((ab) => (
                      <SelectItem key={ab.id} value={ab.id}>
                        <span className="flex items-center gap-1.5">
                          {ab.display_name}
                          {ab.read_only && (
                            // A styled Tooltip renders a focusable trigger,
                            // which has no business nesting inside a listbox
                            // option -- the native title is enough here.
                            <Lock className="h-3 w-3 shrink-0 text-muted-foreground">
                              <title>Read-only</title>
                            </Lock>
                          )}
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
              {groups.length > 0 && (
                // A card's own CATEGORIES and a KIND:group card's own members
                // are the two ways an address book groups people, and a real
                // one uses both -- so both kinds render here, undistinguished,
                // rather than one being offered as the only kind that exists.
                <Select
                  value={groupId ?? ALL_GROUPS_VALUE}
                  onValueChange={(v) => setGroupId(!v || v === ALL_GROUPS_VALUE ? undefined : v)}
                >
                  <SelectTrigger size="sm" className="min-w-0 flex-1">
                    <SelectValue placeholder="All groups">
                      {(v: string) =>
                        v === ALL_GROUPS_VALUE
                          ? "All groups"
                          : (groups.find((g) => g.id === v)?.name ?? "All groups")
                      }
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL_GROUPS_VALUE}>All groups</SelectItem>
                    {groups.map((g) => (
                      <SelectItem key={g.id} value={g.id}>
                        {g.name}
                        <span className="ml-1 opacity-60">{g.count}</span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          ) : null}
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
        // Keyed on what the list is *of*: a changed query, address book or
        // group is a different list, and without a new key the view keeps
        // its old scroll offset, clamped into the middle of a much shorter
        // result set -- so a search over a large book opens somewhere in
        // the middle of its own results. The mail list keys its own VList
        // the same way.
        <VList
          key={`${addressbookId ?? "all"}:${query.trim()}:${groupId ?? "all"}`}
          ref={vlistRef}
          className="flex-1"
          style={{ height: "100%" }}
          onScroll={handleScroll}
        >
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
                  // select-none: a shift-click extending the checkbox range
                  // selects the row's own text as a side effect otherwise --
                  // the mail list's rows have nothing to select in the same
                  // way and don't need this.
                  "group flex w-full cursor-pointer select-none items-center gap-3 border-b px-3 hover:bg-accent/50",
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
                  <Tooltip>
                    <TooltipTrigger className="ml-auto flex shrink-0 items-center">
                      <Lock className="h-3.5 w-3.5 text-muted-foreground" />
                    </TooltipTrigger>
                    <TooltipContent side="left">Read-only</TooltipContent>
                  </Tooltip>
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
