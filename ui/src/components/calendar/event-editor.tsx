"use client";

/**
 * The full event form, in a Sheet (right on desktop, bottom/full on mobile
 * -- the Sheet primitive already switches side by breakpoint the way the
 * rest of the app uses it).
 */

import { useEffect, useState } from "react";
import { Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { RecurrenceScopeDialog } from "@/components/calendar/recurrence-scope-dialog";
import { useCalendars } from "@/hooks/use-calendars";
import { useCreateEvent, useDeleteEvent, useUpdateEvent } from "@/hooks/use-events";
import { useToast } from "@/hooks/use-toast";
import { parseAddressList } from "@/lib/format";
import type { EventInstance, RecurrenceScope } from "@/types/api";

// The Select primitive treats an empty item value as "no selection", so
// "Does not repeat" is represented on the wire as well as here: sending
// rrule="" is what actually removes an existing RRULE (see the comment on
// buildCommonPayload) -- rrule left out of the request entirely leaves
// whatever the event already had untouched, per ical.py's
// _apply_field_overrides.
const RECURRENCE_PRESETS: { label: string; rrule: string }[] = [
  { label: "Does not repeat", rrule: "" },
  { label: "Daily", rrule: "FREQ=DAILY" },
  { label: "Weekly", rrule: "FREQ=WEEKLY" },
  { label: "Monthly", rrule: "FREQ=MONTHLY" },
  { label: "Yearly", rrule: "FREQ=YEARLY" },
];

/** A preset's own value is bare ("FREQ=WEEKLY") but a real event's rrule
 * almost always carries more (COUNT, UNTIL, BYDAY -- every server this
 * app syncs against writes one of those onto an ordinary weekly event).
 * Matching the Select's controlled value by exact string equality would
 * fail on every such event and fall back to the raw RRULE text, so this
 * matches on FREQ alone -- the one axis the four presets actually offer.
 * An unrecognised FREQ (not one of the four) still falls back to the raw
 * value, same as an unmatched value always has. */
function presetSelectValue(rrule: string): string {
  if (rrule === "") return "none";
  const freq = /(?:^|;)FREQ=([A-Z]+)/.exec(rrule)?.[1];
  const preset = freq && RECURRENCE_PRESETS.find((p) => p.rrule === `FREQ=${freq}`);
  return preset ? preset.rrule : rrule;
}

function toLocalInputValue(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromLocalInputValue(value: string): string {
  return new Date(value).toISOString();
}

/** The calendar day an instant falls on in the browser's own local time --
 * what someone looking at a wall-clock time means by "today", unlike the
 * UTC day the same instant can carry near midnight in a positive offset. */
function toLocalDateValue(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** An all-day dtstart/dtend carries no timezone at all (RFC 5545
 * VALUE=DATE) -- the API always encodes the day as literal UTC midnight,
 * so reading it back must read the UTC date directly rather than through
 * whatever zone the browser sits in, or the same stored day would render
 * differently depending on where it's opened. */
function toWholeDayValue(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

function wholeDayIso(value: string): string {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toISOString();
}

function addDaysIso(iso: string, days: number): string {
  const d = new Date(iso);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString();
}

function defaultRange(defaultDate?: Date): { start: string; end: string } {
  const base = defaultDate ? new Date(defaultDate) : new Date();
  base.setMinutes(0, 0, 0);
  base.setHours(base.getHours() + 1);
  const end = new Date(base.getTime() + 60 * 60_000);
  return { start: base.toISOString(), end: end.toISOString() };
}

/** The range as the form displays it -- for an all-day event this is the
 * *inclusive* last day (what someone picking an end date on a calendar
 * expects), one day short of the exclusive dtend RFC 5545 and the API
 * both store. buildCommonPayload() below does the inverse conversion. */
function toDisplayRange(event: EventInstance | undefined, defaultDate?: Date) {
  if (!event) return defaultRange(defaultDate);
  return { start: event.dtstart, end: event.all_day ? addDaysIso(event.dtend, -1) : event.dtend };
}

interface EventEditorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: "create" | "edit";
  event?: EventInstance;
  defaultCalendarId?: string;
  defaultDate?: Date;
  onDeleted?: () => void;
}

export function EventEditor({
  open,
  onOpenChange,
  mode,
  event,
  defaultCalendarId,
  defaultDate,
  onDeleted,
}: EventEditorProps) {
  const { data: calendars } = useCalendars();
  const createEvent = useCreateEvent();
  const updateEvent = useUpdateEvent();
  const deleteEvent = useDeleteEvent();
  const { push: pushToast } = useToast();

  const writableCalendars = (calendars ?? []).filter((c) => !c.read_only);

  const [summary, setSummary] = useState(event?.summary ?? "");
  const [allDay, setAllDay] = useState(event?.all_day ?? false);
  const [range, setRange] = useState(() => toDisplayRange(event, defaultDate));
  const [calendarId, setCalendarId] = useState(
    event?.calendar_id ?? defaultCalendarId ?? writableCalendars[0]?.id ?? "",
  );
  const [location, setLocation] = useState(event?.location ?? "");
  const [description, setDescription] = useState(event?.description ?? "");
  const [rrule, setRrule] = useState(event?.rrule ?? "");
  const [attendees, setAttendees] = useState(
    (event?.attendees ?? []).map((a) => a.email).join(", "),
  );
  const [scopeDialog, setScopeDialog] = useState<"save" | "delete" | null>(null);
  const [confirmSimpleDelete, setConfirmSimpleDelete] = useState(false);

  useEffect(() => {
    if (!open) return;
    setSummary(event?.summary ?? "");
    setAllDay(event?.all_day ?? false);
    setRange(toDisplayRange(event, defaultDate));
    setCalendarId(event?.calendar_id ?? defaultCalendarId ?? writableCalendars[0]?.id ?? "");
    setLocation(event?.location ?? "");
    setDescription(event?.description ?? "");
    setRrule(event?.rrule ?? "");
    setAttendees((event?.attendees ?? []).map((a) => a.email).join(", "));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, event?.object_id, event?.recurrence_id]);

  const calendar = calendars?.find((c) => c.id === calendarId);
  const readOnly = calendar?.read_only ?? false;
  const isRecurring = mode === "edit" && (event?.is_recurring ?? false);

  const handleAllDayChange = (checked: boolean) => {
    setAllDay(checked);
    setRange((r) => {
      if (!checked) return defaultRange(new Date(r.start));
      // A fresh one-day event: both fields start on the same day the
      // timed range was showing, in the browser's own local time -- not
      // the UTC day the same instant could carry near midnight.
      const day = wholeDayIso(toLocalDateValue(r.start));
      return { start: day, end: day };
    });
  };

  // dtend is exclusive (RFC 5545) -- range.end above is the *inclusive*
  // last day the all-day fields display, so it is converted back here,
  // the one place this request is actually built. rrule is always sent
  // as its full current value (never omitted): "" is what removes an
  // existing RRULE, distinct from the field being left out entirely,
  // which ical.py's _apply_field_overrides reads as "unchanged".
  const buildCommonPayload = () => ({
    calendar_id: calendarId,
    summary,
    dtstart: range.start,
    dtend: allDay ? addDaysIso(range.end, 1) : range.end,
    all_day: allDay,
    location: location || undefined,
    description: description || undefined,
    rrule,
  });

  const doSave = (scope?: RecurrenceScope) => {
    if (mode === "create") {
      createEvent.mutate(
        {
          ...buildCommonPayload(),
          // The API refuses tz on an all-day event (it has no time of
          // day to bind) and on any edit (dtstart/dtend already carry
          // the instant there) -- only a timed create ever sends it.
          tz: allDay ? undefined : Intl.DateTimeFormat().resolvedOptions().timeZone,
          attendees: parseAddressList(attendees).map((email) => ({ email })),
        },
        {
          onSuccess: () => {
            pushToast("Event created", "success");
            onOpenChange(false);
          },
          onError: (err) => pushToast(`Could not create event: ${err.message}`, "error", 0),
        },
      );
      return;
    }
    if (!event) return;
    updateEvent.mutate(
      {
        objectId: event.object_id,
        recurrenceId: event.recurrence_id,
        // recurrence_id is only meaningful (and only accepted by the API)
        // alongside scope="this" -- every occurrence, recurring or not,
        // carries a recurrence_id once expanded for display, so gating
        // this on scope rather than on event.recurrence_id being set is
        // what keeps a non-recurring event's edit from being misread as
        // one occurrence of a series with no scope given.
        data: {
          ...buildCommonPayload(),
          scope,
          recurrence_id: scope === "this" ? (event.recurrence_id ?? undefined) : undefined,
        },
      },
      {
        onSuccess: () => {
          pushToast("Event updated", "success");
          onOpenChange(false);
        },
        onError: (err) => pushToast(`Could not update event: ${err.message}`, "error", 0),
      },
    );
  };

  const handleSave = () => {
    if (isRecurring) {
      setScopeDialog("save");
      return;
    }
    doSave();
  };

  const doDelete = (scope?: RecurrenceScope) => {
    if (!event) return;
    deleteEvent.mutate(
      {
        objectId: event.object_id,
        data: { scope, recurrence_id: event.recurrence_id ?? undefined },
      },
      {
        onSuccess: () => {
          pushToast("Event deleted", "success");
          onOpenChange(false);
          onDeleted?.();
        },
        onError: (err) => pushToast(`Could not delete event: ${err.message}`, "error", 0),
      },
    );
  };

  const handleDelete = () => {
    if (isRecurring) {
      setScopeDialog("delete");
      return;
    }
    setConfirmSimpleDelete(true);
  };

  const attendeeCount = parseAddressList(attendees).length;
  const isOrganizerWithGuests = mode === "edit" && event?.organizer === null && attendeeCount > 0;

  return (
    <>
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent side="right" className="w-full sm:max-w-md">
          <SheetHeader>
            <SheetTitle>{mode === "create" ? "New event" : "Edit event"}</SheetTitle>
          </SheetHeader>

          <div className="flex flex-col gap-3 overflow-y-auto px-4 pb-4">
            {readOnly && (
              <p className="rounded-md border bg-muted/30 p-2 text-xs text-muted-foreground">
                This calendar is read-only. Changes cannot be saved.
              </p>
            )}
            <div className="grid gap-1.5">
              <Label htmlFor="event-title">Title</Label>
              <Input
                id="event-title"
                value={summary}
                onChange={(e) => setSummary(e.target.value)}
                disabled={readOnly}
                autoFocus
              />
            </div>

            <div className="flex items-center justify-between">
              <Label htmlFor="event-allday">All day</Label>
              <Switch id="event-allday" checked={allDay} onCheckedChange={handleAllDayChange} disabled={readOnly} />
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div className="grid gap-1.5">
                <Label>Starts</Label>
                <Input
                  type={allDay ? "date" : "datetime-local"}
                  value={allDay ? toWholeDayValue(range.start) : toLocalInputValue(range.start)}
                  disabled={readOnly}
                  onChange={(e) =>
                    setRange((r) => ({
                      ...r,
                      start: allDay ? wholeDayIso(e.target.value) : fromLocalInputValue(e.target.value),
                    }))
                  }
                />
              </div>
              <div className="grid gap-1.5">
                <Label>Ends</Label>
                <Input
                  type={allDay ? "date" : "datetime-local"}
                  value={allDay ? toWholeDayValue(range.end) : toLocalInputValue(range.end)}
                  disabled={readOnly}
                  onChange={(e) =>
                    setRange((r) => ({
                      ...r,
                      end: allDay ? wholeDayIso(e.target.value) : fromLocalInputValue(e.target.value),
                    }))
                  }
                />
              </div>
            </div>

            <div className="grid gap-1.5">
              <Label>Calendar</Label>
              <Select value={calendarId} onValueChange={(v) => v && setCalendarId(v)}>
                <SelectTrigger>
                  <SelectValue placeholder="Choose a calendar" />
                </SelectTrigger>
                <SelectContent>
                  {writableCalendars.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="event-location">Location</Label>
              <Input
                id="event-location"
                value={location}
                onChange={(e) => setLocation(e.target.value)}
                disabled={readOnly}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="event-description">Description</Label>
              <Textarea
                id="event-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={4}
                disabled={readOnly}
              />
            </div>

            <div className="grid gap-1.5">
              <Label>Repeats</Label>
              <Select
                value={presetSelectValue(rrule)}
                onValueChange={(v) => setRrule(v === "none" ? "" : (v as string))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {RECURRENCE_PRESETS.map((p) => (
                    <SelectItem key={p.label} value={p.rrule === "" ? "none" : p.rrule}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="event-attendees">Attendees</Label>
              <Input
                id="event-attendees"
                value={attendees}
                onChange={(e) => setAttendees(e.target.value)}
                placeholder="name@example.com, another@example.com"
                disabled={readOnly || mode === "edit"}
              />
              {mode === "edit" && (
                <p className="text-xs text-muted-foreground">
                  Attendees cannot be changed after an event is created.
                </p>
              )}
            </div>
          </div>

          <SheetFooter className="flex-row items-center justify-between">
            {mode === "edit" ? (
              <Button
                variant="ghost"
                className="text-destructive"
                disabled={readOnly || deleteEvent.isPending}
                onClick={handleDelete}
              >
                <Trash2 className="mr-1 h-4 w-4" />
                Delete
              </Button>
            ) : (
              <span />
            )}
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                disabled={readOnly || !summary || !calendarId || createEvent.isPending || updateEvent.isPending}
                onClick={handleSave}
              >
                {(createEvent.isPending || updateEvent.isPending) && (
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                )}
                Save
              </Button>
            </div>
          </SheetFooter>
        </SheetContent>
      </Sheet>

      <RecurrenceScopeDialog
        open={scopeDialog !== null}
        onOpenChange={(o) => !o && setScopeDialog(null)}
        cancellationNoticeCount={
          scopeDialog === "delete" && isOrganizerWithGuests ? attendeeCount : undefined
        }
        onConfirm={(scope) => {
          setScopeDialog(null);
          if (scopeDialog === "save") doSave(scope);
          else if (scopeDialog === "delete") doDelete(scope);
        }}
      />

      <ConfirmDialog
        open={confirmSimpleDelete}
        onOpenChange={setConfirmSimpleDelete}
        title="Delete this event?"
        description={
          isOrganizerWithGuests
            ? `A cancellation will be sent to ${attendeeCount} guest${attendeeCount === 1 ? "" : "s"}.`
            : "This cannot be undone."
        }
        isConfirming={deleteEvent.isPending}
        onConfirm={() => doDelete()}
      />
    </>
  );
}
