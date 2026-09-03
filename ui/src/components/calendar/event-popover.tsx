"use client";

/**
 * The quick view opened by clicking an event chip anywhere in the calendar:
 * times, calendar, attendees, RSVP, Edit, Delete. Anchored to the chip's
 * bounding rect captured at click time (`eventPopoverAnchorAtom`), since
 * chips live in several different views.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useAtom, useAtomValue } from "jotai";
import { useRouter } from "next/navigation";
import { Loader2, MapPin, Pencil, Trash2, Users, X } from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { RecurrenceScopeDialog } from "@/components/calendar/recurrence-scope-dialog";
import { EventEditor } from "@/components/calendar/event-editor";
import { RsvpControl } from "@/components/calendar/rsvp-control";
import { useCalendars } from "@/hooks/use-calendars";
import { useDeleteEvent, useEventDetail } from "@/hooks/use-events";
import { resolveCalendarColor } from "@/components/calendar/colors";
import { deriveEventLook, isEventOrganizedBySelf } from "@/components/calendar/layout";
import { useIdentities } from "@/hooks/use-identities";
import {
  eventDeleteRequestAtom,
  eventPopoverAnchorAtom,
  selectedEventAtom,
  selectedMailIdAtom,
} from "@/lib/atoms";
import { format } from "@/lib/dates";
import { getInitials } from "@/lib/format";
import type { RecurrenceScope } from "@/types/api";

export function EventPopover() {
  const router = useRouter();
  const [selected, setSelected] = useAtom(selectedEventAtom);
  const [, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const anchor = useAtomValue(eventPopoverAnchorAtom);
  const [deleteRequest, setDeleteRequest] = useAtom(eventDeleteRequestAtom);
  const ref = useRef<HTMLDivElement>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [scopeOpen, setScopeOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const { data: event, isLoading } = useEventDetail(
    selected?.objectId ?? null,
    selected?.recurrenceId ?? null,
  );
  const { data: calendars } = useCalendars();
  const { data: identities } = useIdentities();
  const deleteEvent = useDeleteEvent();
  const calendar = calendars?.find((c) => c.id === event?.calendar_id);
  const calendarIdentityEmail = identities?.find((i) => i.id === calendar?.identity_id)?.address;
  const isRecurring = event?.is_recurring ?? false;
  const cancellationGuestCount =
    event && isEventOrganizedBySelf(event.organizer, calendarIdentityEmail) && event.attendees.length > 0
      ? event.attendees.length
      : undefined;

  const requestDelete = useCallback(() => {
    if (isRecurring) setScopeOpen(true);
    else setConfirmDelete(true);
  }, [isRecurring]);

  // The editor Sheet and the two confirmation dialogs are layers this
  // popover puts on top of itself, and every one of them -- along with
  // anything they open in turn, such as a Select's own popup -- renders
  // into a portal outside this popover's DOM subtree. So while one is up,
  // this popover is not the topmost layer and must not dismiss itself:
  // any press inside the layer would otherwise read as an outside press
  // and unmount the popover, taking the layer down with it before its own
  // click ever completes. Each layer dismisses itself.
  const hasLayerAbove = editorOpen || scopeOpen || confirmDelete;

  useEffect(() => {
    if (hasLayerAbove) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setSelected(null);
    }
    function onPointerDown(e: PointerEvent) {
      if (ref.current?.contains(e.target as Node)) return;
      setSelected(null);
    }
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onPointerDown);
    };
  }, [hasLayerAbove, setSelected]);

  // The Delete key has no direct handle on this popover's own confirmation
  // state, so it comes in as an atom instead -- only acted on once the event
  // it named is the one actually loaded here, and only if it can be deleted
  // at all (the Delete button itself is hidden for a read-only event).
  useEffect(() => {
    if (!deleteRequest || !event || event.read_only) return;
    if (deleteRequest.objectId !== event.object_id) return;
    if (deleteRequest.recurrenceId !== (event.recurrence_id ?? null)) return;
    requestDelete();
    setDeleteRequest(null);
  }, [deleteRequest, event, requestDelete, setDeleteRequest]);

  if (!selected) return null;

  const style: React.CSSProperties = anchor
    ? {
        position: "fixed",
        top: Math.min(anchor.bottom + 6, window.innerHeight - 320),
        left: Math.min(anchor.left, window.innerWidth - 340),
      }
    : { position: "fixed", top: "20%", left: "50%", transform: "translateX(-50%)" };

  const doDelete = (scope?: RecurrenceScope) => {
    if (!event) return;
    deleteEvent.mutate(
      { objectId: event.object_id, data: { scope, recurrence_id: event.recurrence_id ?? undefined } },
      { onSuccess: () => setSelected(null) },
    );
  };

  return (
    <>
      <div
        ref={ref}
        style={style}
        className="z-50 flex w-80 flex-col gap-2 rounded-lg border bg-popover p-3 shadow-lg"
      >
        {isLoading || !event ? (
          <div className="flex items-center justify-center py-6">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <>
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-start gap-2">
                <span
                  className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{
                    background: calendar ? resolveCalendarColor(calendar) : "var(--muted-foreground)",
                  }}
                />
                <div>
                  <p
                    className={
                      deriveEventLook(event).cancelled
                        ? "text-sm font-medium text-muted-foreground line-through"
                        : "text-sm font-medium"
                    }
                  >
                    {deriveEventLook(event).cancelled ? "Cancelled: " : ""}
                    {event.summary || "(no title)"}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {event.all_day
                      ? format(new Date(event.dtstart), "EEE, MMM d") + " · All day"
                      : `${format(new Date(event.dtstart), "EEE, MMM d · HH:mm")} – ${format(new Date(event.dtend), "HH:mm")}`}
                  </p>
                </div>
              </div>
              <Button variant="ghost" size="icon-xs" onClick={() => setSelected(null)}>
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>

            {event.location && (
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <MapPin className="h-3.5 w-3.5 shrink-0" />
                {event.location}
              </div>
            )}

            {calendar && (
              <div className="text-xs text-muted-foreground">{calendar.display_name}</div>
            )}

            {event.attendees.length > 0 && (
              <div className="flex items-start gap-1.5 text-xs">
                <Users className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <div className="flex flex-wrap gap-1">
                  {event.attendees.map((a) => (
                    <div key={a.email} className="flex items-center gap-1">
                      <Avatar size="sm">
                        <AvatarFallback>{getInitials(a.cn || a.email)}</AvatarFallback>
                      </Avatar>
                      <span className="text-muted-foreground">{a.cn || a.email}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {event.partstat !== null && (
              <div className="border-t pt-2">
                <RsvpControl event={event} />
              </div>
            )}

            <div className="flex items-center justify-between border-t pt-2">
              <div className="flex gap-1">
                {!event.read_only && (
                  <Button variant="ghost" size="sm" onClick={() => setEditorOpen(true)}>
                    <Pencil className="mr-1 h-3.5 w-3.5" />
                    Edit
                  </Button>
                )}
                {!event.read_only && (
                  <Button variant="ghost" size="sm" className="text-destructive" onClick={requestDelete}>
                    <Trash2 className="mr-1 h-3.5 w-3.5" />
                    Delete
                  </Button>
                )}
              </div>
              {event.source_message_id && (
                <Button
                  variant="link"
                  size="sm"
                  className="h-auto p-0 text-xs"
                  onClick={() => {
                    setSelectedMailId(event.source_message_id);
                    setSelected(null);
                    router.push("/");
                  }}
                >
                  Open invitation email
                </Button>
              )}
            </div>
          </>
        )}
      </div>

      {event && (
        <EventEditor
          open={editorOpen}
          onOpenChange={setEditorOpen}
          mode="edit"
          event={event}
          onDeleted={() => setSelected(null)}
        />
      )}

      <RecurrenceScopeDialog
        open={scopeOpen}
        onOpenChange={setScopeOpen}
        cancellationNoticeCount={cancellationGuestCount}
        onConfirm={(scope) => {
          setScopeOpen(false);
          doDelete(scope);
        }}
      />

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete this event?"
        description={
          cancellationGuestCount !== undefined
            ? `A cancellation will be sent to ${cancellationGuestCount} guest${cancellationGuestCount === 1 ? "" : "s"}. This cannot be undone.`
            : "This cannot be undone."
        }
        isConfirming={deleteEvent.isPending}
        onConfirm={() => doDelete()}
      />
    </>
  );
}
