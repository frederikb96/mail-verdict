"use client";

/** Create/delete calendars and edit a calendar's name, colour, identity link
 * and invitation intake -- the same facts calendar-links.tsx edits per
 * identity, here edited per calendar. */

import { useEffect, useState } from "react";
import { Loader2, Plus, Settings2, Trash2 } from "lucide-react";
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
import { useIdentities } from "@/hooks/use-identities";
import { cn } from "@/lib/utils";
import type { Calendar, CalendarIntake } from "@/types/api";

const INTAKE_LABELS: Record<CalendarIntake, string> = {
  none: "Do nothing with invitations",
  import_and_link: "Import invitations automatically",
};

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
  const { data: identities } = useIdentities();
  const [confirmDelete, setConfirmDelete] = useState(false);

  return (
    <div className="flex flex-col gap-2 rounded-md border p-2">
      <div className="flex items-center gap-2">
        <ColorPicker calendar={calendar} />
        <span className="flex-1 truncate text-sm">{calendar.display_name}</span>
        {calendar.read_only && <span className="text-xs text-muted-foreground">Read-only</span>}
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
          className="text-destructive"
          onClick={() => setConfirmDelete(true)}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="grid gap-1">
          <Label className="text-xs">Identity</Label>
          <Select
            value={calendar.identity_id ?? "none"}
            onValueChange={(v) =>
              updateCalendar.mutate({
                id: calendar.id,
                data: { identity_id: v === "none" ? null : v },
              })
            }
          >
            <SelectTrigger size="sm">
              <SelectValue>
                {(v: string) => (v === "none" ? "None" : (identities?.find((i) => i.id === v)?.address ?? v))}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">None</SelectItem>
              {identities?.map((i) => (
                <SelectItem key={i.id} value={i.id}>
                  {i.address}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-1">
          <Label className="text-xs">Invitations</Label>
          <Select
            value={calendar.intake}
            onValueChange={(v) =>
              v && updateCalendar.mutate({ id: calendar.id, data: { intake: v as CalendarIntake } })
            }
          >
            <SelectTrigger size="sm">
              <SelectValue>{(v: CalendarIntake) => INTAKE_LABELS[v] ?? v}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(INTAKE_LABELS) as CalendarIntake[]).map((k) => (
                <SelectItem key={k} value={k}>
                  {INTAKE_LABELS[k]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
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
      className="flex items-end gap-2"
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
      <div className="grid gap-1">
        <Label className="text-xs">Server</Label>
        <Select value={davAccountId} onValueChange={(v) => v && setDavAccountId(v)}>
          <SelectTrigger className="w-40">
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
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Manage calendars</DialogTitle>
        </DialogHeader>
        <div className="flex max-h-96 flex-col gap-2 overflow-y-auto">
          {calendars?.map((c) => (
            <CalendarRow key={c.id} calendar={c} />
          ))}
          {calendars?.length === 0 && (
            <p className="py-4 text-center text-sm text-muted-foreground">No calendars yet</p>
          )}
        </div>
        <NewCalendarForm />
      </DialogContent>
    </Dialog>
  );
}
