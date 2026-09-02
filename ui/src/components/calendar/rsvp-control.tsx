"use client";

/**
 * Accept / Tentative / Decline, shared by the invitation card and the
 * calendar popover so a reply that never left reads identically everywhere.
 * The backend writes PARTSTAT immediately even while the reply itself is
 * still in flight, so filling the clicked button optimistically is honest,
 * not optimistic in the risky sense.
 */

import { useState } from "react";
import { Check, HelpCircle, Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { useRespond } from "@/hooks/use-events";
import { useCalendars } from "@/hooks/use-calendars";
import { useIdentities } from "@/hooks/use-identities";
import type { EventInstance, Partstat } from "@/types/api";

const OPTIONS: { value: "accepted" | "tentative" | "declined"; label: string; icon: typeof Check }[] = [
  { value: "accepted", label: "Accept", icon: Check },
  { value: "tentative", label: "Tentative", icon: HelpCircle },
  { value: "declined", label: "Decline", icon: X },
];

interface RsvpControlProps {
  event: EventInstance;
}

export function RsvpControl({ event }: RsvpControlProps) {
  const respond = useRespond();
  const { data: calendars } = useCalendars();
  const { data: identities } = useIdentities();
  const [showComment, setShowComment] = useState(false);
  const [comment, setComment] = useState("");

  const calendar = calendars?.find((c) => c.id === event.calendar_id);
  const identity = identities?.find((i) => i.id === calendar?.identity_id);
  const partstat: Partstat | null = event.partstat;
  const ownReply = event.own_reply;

  const send = (value: "accepted" | "tentative" | "declined") => {
    if (!identity) return;
    respond.mutate({
      objectId: event.object_id,
      recurrenceId: event.recurrence_id,
      data: {
        identity_id: identity.id,
        partstat: value,
        comment: comment.trim() || undefined,
      },
    });
  };

  return (
    <div data-testid="rsvp" data-partstat={partstat ?? ""} className="flex flex-col gap-1.5">
      <div className="flex gap-1">
        {OPTIONS.map(({ value, label, icon: Icon }) => (
          <Button
            key={value}
            size="sm"
            variant={partstat === value ? "default" : "outline"}
            className={cn("h-9 flex-1 gap-1.5", "sm:h-7")}
            disabled={!identity || respond.isPending}
            onClick={() => send(value)}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </Button>
        ))}
      </div>

      {!identity && (
        <span className="text-xs text-muted-foreground">
          This calendar has no identity to reply from.
        </span>
      )}

      {identity && ownReply?.outbox_status && (ownReply.outbox_status === "pending" || ownReply.outbox_status === "processing") && (
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          Sending reply from {identity.address}…
        </span>
      )}
      {ownReply?.outbox_status === "sent" && (
        <span className="text-xs text-muted-foreground">Reply sent</span>
      )}
      {ownReply?.outbox_status === "failed" && (
        <span className="text-xs text-muted-foreground">Retrying…</span>
      )}
      {ownReply?.outbox_status === "dead" && (
        <div className="rounded-md bg-destructive/10 p-2 text-xs text-destructive">
          <p>
            You {partstat === "accepted" ? "accepted" : "responded"}, but the organizer has not
            been told. The reply could not be sent
            {ownReply.error ? `: ${ownReply.error}` : "."}
          </p>
          <div className="mt-1 flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 text-xs"
              disabled={respond.isPending}
              onClick={() => send((partstat ?? "accepted") as "accepted" | "tentative" | "declined")}
            >
              Send again
            </Button>
          </div>
        </div>
      )}

      {!showComment ? (
        <button
          type="button"
          className="w-fit text-xs text-muted-foreground hover:text-foreground"
          onClick={() => setShowComment(true)}
        >
          Add a note to the organizer
        </button>
      ) : (
        <Textarea
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="Note to the organizer"
          rows={2}
          className="text-xs"
        />
      )}
    </div>
  );
}
