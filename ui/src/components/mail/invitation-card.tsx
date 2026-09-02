"use client";

/**
 * The card rendered inside a mail message when it carries a calendar
 * invitation (`text/calendar` / `application/ics`). This is where the
 * calendar stops being generic and becomes a mail client's calendar --
 * every status the backend can produce reads as one sentence and one
 * action, never a button that would always fail (a forwarded invitation
 * offers no RSVP, since the identity was never among the attendees).
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useSetAtom } from "jotai";
import { CalendarDays, Loader2, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { RsvpControl } from "@/components/calendar/rsvp-control";
import { resolveCalendarColor } from "@/components/calendar/colors";
import { useCalendars } from "@/hooks/use-calendars";
import { useEventDetail } from "@/hooks/use-events";
import { useImportInvitation, useInvitation } from "@/hooks/use-invitation";
import { calendarDateAtom, selectedEventAtom } from "@/lib/atoms";
import { format } from "@/lib/dates";
import { cn } from "@/lib/utils";

interface InvitationCardProps {
  messageId: string;
}

export function InvitationCard({ messageId }: InvitationCardProps) {
  const router = useRouter();
  const { data: invitation, isLoading } = useInvitation(messageId);
  const { data: calendars } = useCalendars();
  const importInvitation = useImportInvitation();
  const setCalendarDate = useSetAtom(calendarDateAtom);
  const setSelectedEvent = useSetAtom(selectedEventAtom);
  const [chosenCalendarId, setChosenCalendarId] = useState<string | undefined>();
  const [alwaysUse, setAlwaysUse] = useState(false);

  const { data: fullEvent } = useEventDetail(
    invitation?.status === "imported" || invitation?.status === "updated"
      ? invitation.object_id
      : null,
    null,
  );

  if (isLoading || !invitation) return null;

  const calendar = calendars?.find((c) => c.id === invitation.calendar_id);
  const writableCalendars = (calendars ?? []).filter((c) => !c.read_only);
  const isForwarded = invitation.own_address === null && invitation.method === "REQUEST";

  const openInCalendar = () => {
    setCalendarDate(new Date(invitation.dtstart));
    if (invitation.object_id) {
      setSelectedEvent({ objectId: invitation.object_id, recurrenceId: null });
    }
    router.push("/calendar");
  };

  return (
    <div
      className={cn(
        "mx-4 mb-3 flex flex-col gap-2 rounded-lg border-l-4 bg-muted/20 p-3",
        invitation.status === "cancelled" && "border-l-destructive bg-destructive/5",
      )}
      style={{
        borderLeftColor:
          invitation.status !== "cancelled"
            ? calendar
              ? resolveCalendarColor(calendar)
              : "var(--muted-foreground)"
            : undefined,
      }}
    >
      <div className="flex items-start gap-2">
        <CalendarDays className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="flex-1">
          <p className="text-sm font-medium">{invitation.summary || "(no title)"}</p>
          <p className="text-xs text-muted-foreground">
            {invitation.all_day
              ? format(new Date(invitation.dtstart), "EEE, MMM d") + " · All day"
              : `${format(new Date(invitation.dtstart), "EEE, MMM d · HH:mm")}–${format(new Date(invitation.dtend), "HH:mm")}`}
          </p>
          {invitation.location && (
            <p className="text-xs text-muted-foreground">{invitation.location}</p>
          )}
          {invitation.organizer && (
            <p className="text-xs text-muted-foreground">
              Invitation from {invitation.organizer.cn || invitation.organizer.email}
            </p>
          )}
          {invitation.attendees.length > 0 && (
            <p className="flex items-center gap-1 text-xs text-muted-foreground">
              <Users className="h-3 w-3" />
              {invitation.attendees.length} attendee{invitation.attendees.length === 1 ? "" : "s"}
              {invitation.own_address && ` — you, as ${invitation.own_address}`}
            </p>
          )}
        </div>
      </div>

      {(invitation.status === "imported" || invitation.status === "updated") && calendar && (
        <div className="text-xs">
          {invitation.status === "updated" ? "Updated in " : "Added to "}
          <span className="font-medium">{calendar.display_name}</span>
          {invitation.status === "updated" && ` (version ${invitation.sequence})`}
        </div>
      )}

      {(invitation.status === "imported" || invitation.status === "updated") &&
        fullEvent &&
        fullEvent.partstat !== null && <RsvpControl event={fullEvent} />}

      {(invitation.status === "imported" || invitation.status === "updated") && (
        <Button variant="link" size="sm" className="h-auto w-fit p-0 text-xs" onClick={openInCalendar}>
          Open in calendar
        </Button>
      )}

      {invitation.status === "unlinked" && !isForwarded && (
        <div className="flex flex-col gap-2">
          <span className="text-xs text-muted-foreground">Not in a calendar yet</span>
          <div className="flex items-center gap-2">
            <Select value={chosenCalendarId} onValueChange={(v) => v && setChosenCalendarId(v)}>
              <SelectTrigger size="sm" className="w-48">
                <SelectValue placeholder="Add to calendar…" />
              </SelectTrigger>
              <SelectContent>
                {writableCalendars.map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              size="sm"
              disabled={!chosenCalendarId || importInvitation.isPending}
              onClick={() =>
                chosenCalendarId &&
                importInvitation.mutate({
                  messageId,
                  data: { calendar_id: chosenCalendarId, link: alwaysUse },
                })
              }
            >
              {importInvitation.isPending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
              Add
            </Button>
          </div>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={alwaysUse}
              onChange={(e) => setAlwaysUse(e.target.checked)}
              className="h-3 w-3"
            />
            Always use this calendar for {invitation.organizer?.email ?? "this sender"}
          </label>
        </div>
      )}

      {invitation.status === "unlinked" && isForwarded && (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">
            You were not invited directly, so a reply cannot be sent.
          </span>
          <div className="flex items-center gap-2">
            <Select value={chosenCalendarId} onValueChange={(v) => v && setChosenCalendarId(v)}>
              <SelectTrigger size="sm" className="w-48">
                <SelectValue placeholder="Add to calendar…" />
              </SelectTrigger>
              <SelectContent>
                {writableCalendars.map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              size="sm"
              disabled={!chosenCalendarId || importInvitation.isPending}
              onClick={() =>
                chosenCalendarId &&
                importInvitation.mutate({ messageId, data: { calendar_id: chosenCalendarId } })
              }
            >
              {importInvitation.isPending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
              Add
            </Button>
          </div>
        </div>
      )}

      {invitation.status === "cancelled" && (
        <p className="text-xs text-destructive">This event was cancelled by the organizer.</p>
      )}

      {invitation.status === "unauthorized" && (
        <div className="text-xs text-destructive">
          This message claims to update an event but was not sent by its organizer, so it was
          not applied.{" "}
          <Button variant="link" size="sm" className="h-auto p-0 text-xs" onClick={openInCalendar}>
            View the event
          </Button>
        </div>
      )}

      {invitation.status === "ignored_stale" && (
        <div className="text-xs text-muted-foreground">
          Outdated — a newer version of this invitation has already been applied.{" "}
          <Button variant="link" size="sm" className="h-auto p-0 text-xs" onClick={openInCalendar}>
            View the event
          </Button>
        </div>
      )}

      {invitation.status === "failed" && (
        <div className="flex items-center justify-between gap-2 text-xs text-destructive">
          <span>
            Could not add to {calendar?.display_name ?? "the calendar"}
            {invitation.error ? `: ${invitation.error}` : "."}
          </span>
          <Button
            variant="outline"
            size="sm"
            className="h-6 px-2 text-xs"
            disabled={!chosenCalendarId || importInvitation.isPending}
            onClick={() =>
              invitation.calendar_id &&
              importInvitation.mutate({ messageId, data: { calendar_id: invitation.calendar_id } })
            }
          >
            Retry
          </Button>
        </div>
      )}

      {invitation.method === "REPLY" && (
        <div className="flex flex-col gap-1 text-xs">
          {invitation.attendees.map((a) => (
            <div key={a.email} className="flex items-center justify-between">
              <span>{a.cn || a.email}</span>
              <span className="text-muted-foreground">{a.partstat}</span>
            </div>
          ))}
          <Button variant="link" size="sm" className="h-auto w-fit p-0 text-xs" onClick={openInCalendar}>
            Open event
          </Button>
        </div>
      )}
    </div>
  );
}
