"use client";

/** Create/delete calendars and edit a calendar's name and colour. Which
 * identity a calendar belongs to, and which one receives invitations, is
 * edited on the Settings page instead -- that surface already validates
 * the one invariant this dialog used to let a person violate (an identity
 * with linked calendars but none chosen to receive invitations). */

import { useEffect, useState } from "react";
import Link from "next/link";
import { Loader2, Pencil, Plus, Settings2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { CALENDAR_PALETTE, resolveCalendarColor } from "@/components/calendar/colors";
import { useCalendars, useCreateCalendar, useDeleteCalendar, useUpdateCalendar } from "@/hooks/use-calendars";
import { useDavAccounts } from "@/hooks/use-dav-accounts";
import { cn } from "@/lib/utils";
import type { Calendar } from "@/types/api";

/** A single swatch that opens the palette in a popover, instead of every
 * calendar permanently rendering all twelve colours as a row of buttons --
 * that laid the name out in an ellipsis and forced the dialog to scroll
 * sideways as well as down. */
function ColorPicker({ calendar }: { calendar: Calendar }) {
  const updateCalendar = useUpdateCalendar();
  const [open, setOpen] = useState(false);
  const current = resolveCalendarColor(calendar);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <button
            type="button"
            aria-label={`Colour for ${calendar.display_name}`}
            className="h-4 w-4 shrink-0 rounded-full border"
            style={{ background: current }}
          />
        }
      />
      <PopoverContent className="w-auto p-2">
        <div className="grid grid-cols-6 gap-1.5">
          {CALENDAR_PALETTE.map((c) => (
            <button
              key={c}
              type="button"
              aria-label={`Colour ${c}`}
              className={cn(
                "h-5 w-5 rounded-full border",
                current === c && "ring-2 ring-ring ring-offset-1",
              )}
              style={{ background: c }}
              onClick={() => {
                updateCalendar.mutate({ id: calendar.id, data: { color_override: c } });
                setOpen(false);
              }}
            />
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

function CalendarRow({ calendar }: { calendar: Calendar }) {
  const updateCalendar = useUpdateCalendar();
  const deleteCalendar = useDeleteCalendar();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(calendar.display_name);

  // The server's own name can change (another client renamed it, or the
  // update above just landed) -- re-seed the local draft whenever it does,
  // the same re-resolve-on-load fix a query-backed default needs elsewhere
  // in this app, except here the query was already loaded and the value
  // simply changed under it.
  useEffect(() => {
    setName(calendar.display_name);
  }, [calendar.display_name]);

  const commitRename = () => {
    setRenaming(false);
    const trimmed = name.trim();
    if (!trimmed || trimmed === calendar.display_name) {
      setName(calendar.display_name);
      return;
    }
    updateCalendar.mutate({ id: calendar.id, data: { display_name: trimmed } });
  };

  return (
    <div className="flex flex-col gap-2 rounded-md border p-2">
      <div className="flex items-center gap-2">
        <ColorPicker calendar={calendar} />
        {renaming ? (
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
              if (e.key === "Escape") {
                setName(calendar.display_name);
                setRenaming(false);
              }
            }}
            className="h-7 min-w-0 flex-1"
          />
        ) : (
          <button
            type="button"
            className="flex min-w-0 flex-1 items-center gap-1 truncate text-left text-sm"
            onClick={() => setRenaming(true)}
          >
            <span className="truncate">{calendar.display_name}</span>
            <Pencil className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden />
            <span className="sr-only">Rename {calendar.display_name}</span>
          </button>
        )}
        {calendar.read_only && (
          <span className="shrink-0 text-xs text-muted-foreground">Read-only</span>
        )}
        <Switch
          aria-label={`Show ${calendar.display_name} in the sidebar`}
          checked={calendar.is_enabled}
          onCheckedChange={(checked) =>
            updateCalendar.mutate({ id: calendar.id, data: { is_enabled: checked } })
          }
        />
        <Button
          variant="ghost"
          size="icon-xs"
          className="shrink-0 text-destructive"
          onClick={() => setConfirmDelete(true)}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete "${calendar.display_name}"?`}
        description={
          `This destroys ${calendar.total_count} event${calendar.total_count === 1 ? "" : "s"} ` +
          "on the mail server. It cannot be undone."
        }
        isConfirming={deleteCalendar.isPending}
        onConfirm={() =>
          deleteCalendar.mutate(
            { id: calendar.id, eventCount: calendar.total_count },
            { onSuccess: () => setConfirmDelete(false) },
          )
        }
      />
    </div>
  );
}

function NewCalendarForm() {
  const { data: davAccounts } = useDavAccounts();
  const createCalendar = useCreateCalendar();
  const [name, setName] = useState("");
  // A defined string from the first render on, never `undefined`: a
  // base-ui Select decides controlled-vs-uncontrolled once, on mount, from
  // whether `value` is `undefined` then -- and never looks again. Passing
  // `undefined` here because davAccounts hasn't loaded yet would lock the
  // trigger into uncontrolled mode, so setting a real id once it loads
  // would update this state without the Select ever rendering it.
  const [davAccountId, setDavAccountId] = useState<string>(davAccounts?.[0]?.id ?? "");

  // davAccounts is still empty on first mount -- re-resolve once it loads,
  // the same fix contact-editor.tsx's own default address book needed.
  useEffect(() => {
    setDavAccountId((current) => current || davAccounts?.[0]?.id || "");
  }, [davAccounts]);

  return (
    <form
      className="flex flex-col gap-2 sm:flex-row sm:items-end"
      onSubmit={(e) => {
        e.preventDefault();
        if (!name || !davAccountId) return;
        createCalendar.mutate(
          { dav_account_id: davAccountId, display_name: name },
          { onSuccess: () => setName("") },
        );
      }}
    >
      <div className="grid flex-1 gap-1">
        <Label className="text-xs">New calendar name</Label>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Personal" />
      </div>
      <div className="grid gap-1 sm:w-40">
        <Label className="text-xs">Server</Label>
        <Select value={davAccountId} onValueChange={(v) => v && setDavAccountId(v)}>
          <SelectTrigger className="w-full">
            <SelectValue placeholder="Choose">
              {(v: string) => davAccounts?.find((a) => a.id === v)?.name ?? "Choose"}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {davAccounts?.map((a) => (
              <SelectItem key={a.id} value={a.id}>
                {a.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <Button type="submit" disabled={createCalendar.isPending || !name || !davAccountId}>
        {createCalendar.isPending ? (
          <Loader2 className="mr-1 h-4 w-4 animate-spin" />
        ) : (
          <Plus className="mr-1 h-4 w-4" />
        )}
        Add
      </Button>
    </form>
  );
}

export function CalendarManageDialog() {
  const { data: calendars } = useCalendars();
  const [open, setOpen] = useState(false);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="ghost" size="icon-xs" aria-label="Manage calendars" />}>
        <Settings2 className="h-3.5 w-3.5" />
      </DialogTrigger>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Manage calendars</DialogTitle>
        </DialogHeader>
        <div className="flex max-h-96 flex-col gap-2 overflow-y-auto overflow-x-hidden">
          {calendars?.map((c) => (
            <CalendarRow key={c.id} calendar={c} />
          ))}
          {calendars?.length === 0 && (
            <p className="py-4 text-center text-sm text-muted-foreground">No calendars yet</p>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          Which identity a calendar belongs to, and which one receives its invitations, is set on
          the{" "}
          <Link href="/settings#calendar" className="underline">
            Settings
          </Link>{" "}
          page.
        </p>
        <NewCalendarForm />
      </DialogContent>
    </Dialog>
  );
}
