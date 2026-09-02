"use client";

/** Create/delete calendars and edit a calendar's name, colour, identity link
 * and invitation intake -- the same facts calendar-links.tsx edits per
 * identity, here edited per calendar. */

import { useState } from "react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CALENDAR_PALETTE } from "@/components/calendar/colors";
import { useCalendars, useCreateCalendar, useDeleteCalendar, useUpdateCalendar } from "@/hooks/use-calendars";
import { useDavAccounts } from "@/hooks/use-dav-accounts";
import { useIdentities } from "@/hooks/use-identities";
import { cn } from "@/lib/utils";
import type { Calendar, CalendarIntake } from "@/types/api";

const INTAKE_LABELS: Record<CalendarIntake, string> = {
  none: "Do nothing with invitations",
  import_and_link: "Import invitations automatically",
};

function CalendarRow({ calendar }: { calendar: Calendar }) {
  const updateCalendar = useUpdateCalendar();
  const deleteCalendar = useDeleteCalendar();
  const { data: identities } = useIdentities();
  const [confirmDelete, setConfirmDelete] = useState(false);

  return (
    <div className="flex flex-col gap-2 rounded-md border p-2">
      <div className="flex items-center gap-2">
        <div className="flex gap-1">
          {CALENDAR_PALETTE.map((c) => (
            <button
              key={c}
              type="button"
              aria-label={`Colour ${c}`}
              className={cn(
                "h-4 w-4 rounded-full border",
                (calendar.color_override ?? calendar.color) === c && "ring-2 ring-ring ring-offset-1",
              )}
              style={{ background: c }}
              onClick={() => updateCalendar.mutate({ id: calendar.id, data: { color_override: c } })}
            />
          ))}
        </div>
        <span className="flex-1 truncate text-sm">{calendar.display_name}</span>
        {calendar.read_only && <span className="text-xs text-muted-foreground">Read-only</span>}
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
              <SelectValue />
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
              <SelectValue />
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
  const [davAccountId, setDavAccountId] = useState<string | undefined>(davAccounts?.[0]?.id);

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
            <SelectValue placeholder="Choose" />
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
