"use client";

/**
 * The identity-to-calendar mapping: one row per identity, a chip-based
 * multi-select of the calendars it can import invitations into, and a
 * dropdown for which one of those receives new invitations by default.
 * Grouped by the mail account the identity belongs to. Dirty-tracked and
 * saved like `CategorySettings` on this same page; a `PUT` names
 * `base_revision` so a conflicting concurrent edit is rejected rather than
 * silently overwritten.
 *
 * A column-per-calendar table was the previous shape here -- it worked for
 * a handful of calendars and became wider than the screen well before
 * thirty. A chip picker scales with however many are actually linked to an
 * identity, not with how many exist.
 */

import { useEffect, useState } from "react";
import { Code, Loader2, Save, Table2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Combobox,
  ComboboxChip,
  ComboboxChipRemove,
  ComboboxChips,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
} from "@/components/ui/combobox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useAccounts } from "@/hooks/use-accounts";
import { useCalendarLinks, useCalendars, useUpdateCalendarLinks } from "@/hooks/use-calendars";
import { useToast } from "@/hooks/use-toast";
import { resolveCalendarColor } from "@/components/calendar/colors";
import { cn } from "@/lib/utils";
import type { Calendar, CalendarLinkRow } from "@/types/api";

function CalendarDot({ calendar }: { calendar: Calendar }) {
  return (
    <span
      className="h-2 w-2 shrink-0 rounded-full"
      style={{ background: resolveCalendarColor(calendar) }}
    />
  );
}

function IdentityLinkRow({
  row,
  calendars,
  pickableCalendars,
  invalid,
  onCalendarIdsChange,
  onReceivingChange,
}: {
  row: CalendarLinkRow;
  calendars: Calendar[];
  pickableCalendars: Calendar[];
  invalid: boolean;
  onCalendarIdsChange: (calendarIds: string[]) => void;
  onReceivingChange: (calendarId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const selected = calendars.filter((c) => row.calendar_ids.includes(c.id));

  return (
    <div
      className={cn(
        "flex flex-col gap-1.5 rounded-md border p-2",
        invalid && "border-destructive bg-destructive/10",
      )}
    >
      <span className="truncate text-sm font-medium">{row.identity_address}</span>

      <Combobox
        multiple
        items={pickableCalendars.map((c) => c.id)}
        value={row.calendar_ids}
        onValueChange={(next) => onCalendarIdsChange(next as string[])}
        inputValue={query}
        onInputValueChange={setQuery}
        itemToStringLabel={(id) => calendars.find((c) => c.id === id)?.display_name ?? id}
      >
        <ComboboxChips>
          {selected.map((c) => (
            <ComboboxChip key={c.id}>
              <CalendarDot calendar={c} />
              {c.display_name}
              <ComboboxChipRemove
                onClick={() => onCalendarIdsChange(row.calendar_ids.filter((id) => id !== c.id))}
              />
            </ComboboxChip>
          ))}
          <ComboboxInput placeholder={selected.length === 0 ? "Link a calendar..." : undefined} />
        </ComboboxChips>
        <ComboboxContent>
          <ComboboxEmpty>No matching calendar</ComboboxEmpty>
          {pickableCalendars.map((c, index) => (
            <ComboboxItem key={c.id} value={c.id} index={index}>
              <CalendarDot calendar={c} />
              {c.display_name}
            </ComboboxItem>
          ))}
        </ComboboxContent>
      </Combobox>

      <Select
        value={row.receives_invitations_calendar_id ?? ""}
        onValueChange={(v) => v && onReceivingChange(v)}
        disabled={selected.length === 0}
      >
        <SelectTrigger size="sm" className="w-full">
          <SelectValue>
            {(v: string) =>
              selected.length === 0
                ? "No calendar linked yet"
                : (selected.find((c) => c.id === v)?.display_name ?? "Which one receives invitations?")
            }
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          {selected.map((c) => (
            <SelectItem key={c.id} value={c.id}>
              {c.display_name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

export function CalendarLinksCard() {
  const { data: links, isLoading } = useCalendarLinks();
  const { data: calendars } = useCalendars();
  const { data: accounts } = useAccounts();
  const updateLinks = useUpdateCalendarLinks();
  const { push: pushToast } = useToast();

  const [rows, setRows] = useState<CalendarLinkRow[]>([]);
  const [dirty, setDirty] = useState(false);
  const [rawMode, setRawMode] = useState(false);
  const [rawText, setRawText] = useState("");
  const [rawError, setRawError] = useState<string | null>(null);
  const [invalidIdentityId, setInvalidIdentityId] = useState<string | null>(null);

  useEffect(() => {
    if (!links) return;
    setRows(links.rows);
    setRawText(JSON.stringify(links.rows, null, 2));
    setDirty(false);
  }, [links]);

  if (isLoading || !links) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Calendar invitations</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-32 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (rows.length === 0) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Calendar invitations</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            No sending identities yet -- add one on the Accounts page to link it to a calendar.
          </p>
        </CardContent>
      </Card>
    );
  }

  const allCalendars = calendars ?? [];
  // A read-only calendar can't take a new event, so it was never a valid
  // choice here -- excluded from what the picker offers rather than shown
  // and then rejected on save.
  const pickableCalendars = allCalendars.filter((c) => !c.read_only);

  const setCalendarIds = (identityId: string, calendarIds: string[]) => {
    setRows((prev) =>
      prev.map((r) => {
        if (r.identity_id !== identityId) return r;
        const receives_invitations_calendar_id = calendarIds.includes(
          r.receives_invitations_calendar_id ?? "",
        )
          ? r.receives_invitations_calendar_id
          : (calendarIds[0] ?? null);
        return { ...r, calendar_ids: calendarIds, receives_invitations_calendar_id };
      }),
    );
    setDirty(true);
  };

  const setReceiving = (identityId: string, calendarId: string) => {
    setRows((prev) =>
      prev.map((r) =>
        r.identity_id === identityId ? { ...r, receives_invitations_calendar_id: calendarId } : r,
      ),
    );
    setDirty(true);
  };

  const handleSave = () => {
    setInvalidIdentityId(null);
    let payloadRows = rows;
    if (rawMode) {
      try {
        payloadRows = JSON.parse(rawText);
        setRawError(null);
      } catch {
        setRawError("Invalid JSON");
        return;
      }
    }
    updateLinks.mutate(
      {
        base_revision: links.base_revision,
        rows: payloadRows.map((r) => ({
          identity_id: r.identity_id,
          calendar_ids: r.calendar_ids,
          receives_invitations_calendar_id: r.receives_invitations_calendar_id,
        })),
      },
      {
        onSuccess: () => setDirty(false),
        onError: (err) => {
          // A 422 names the offending identity so its row can be
          // highlighted -- best-effort parse, since the shape is the
          // backend's error body, not a typed response.
          try {
            const body = JSON.parse(err.message) as { identity_id?: string };
            if (body.identity_id) setInvalidIdentityId(body.identity_id);
          } catch {
            // Not JSON -- nothing to highlight.
          }
          pushToast(`Could not save: ${err.message}`, "error", 0);
        },
      },
    );
  };

  const groupedByAccount = new Map<string, CalendarLinkRow[]>();
  for (const r of rows) {
    if (!groupedByAccount.has(r.account_id)) groupedByAccount.set(r.account_id, []);
    groupedByAccount.get(r.account_id)!.push(r);
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between pb-3">
        <CardTitle className="text-base">Calendar invitations</CardTitle>
        <Button variant="ghost" size="icon-sm" onClick={() => setRawMode(!rawMode)}>
          {rawMode ? <Table2 className="h-4 w-4" /> : <Code className="h-4 w-4" />}
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!rawMode ? (
          <>
            {Array.from(groupedByAccount.entries()).map(([accountId, identityRows]) => (
              <div key={accountId} className="flex flex-col gap-2">
                <span className="text-xs font-medium text-muted-foreground">
                  {accounts?.find((a) => a.id === accountId)?.name ?? accountId}
                </span>
                <div className="grid gap-2 sm:grid-cols-2">
                  {identityRows.map((r) => (
                    <IdentityLinkRow
                      key={r.identity_id}
                      row={r}
                      calendars={allCalendars}
                      pickableCalendars={pickableCalendars}
                      invalid={invalidIdentityId === r.identity_id}
                      onCalendarIdsChange={(ids) => setCalendarIds(r.identity_id, ids)}
                      onReceivingChange={(id) => setReceiving(r.identity_id, id)}
                    />
                  ))}
                </div>
              </div>
            ))}
            {pickableCalendars.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No writable calendar exists yet -- add one through Manage calendars in the
                calendar sidebar.
              </p>
            )}
          </>
        ) : (
          <div className="flex flex-col gap-1.5">
            <Textarea
              value={rawText}
              onChange={(e) => {
                setRawText(e.target.value);
                setDirty(true);
              }}
              rows={12}
              className="font-mono text-xs"
            />
            {rawError && <p className="text-xs text-destructive">{rawError}</p>}
          </div>
        )}

        {dirty && (
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={updateLinks.isPending}>
              {updateLinks.isPending ? (
                <Loader2 className="mr-1 h-4 w-4 animate-spin" />
              ) : (
                <Save className="mr-1 h-4 w-4" />
              )}
              Save
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
