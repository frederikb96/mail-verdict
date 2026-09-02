"""
Turning an emailed calendar invitation into a calendar entry.

A listener like spam/feedback.py, not a pipeline stage: it reacts to
`message`/`insert` with `origin = "sync"`, the same event
pipeline/enqueue.py's enqueue_live_arrival reacts to, and for the same
reason backfilled mail never reaches it -- the contract's backfill
suppression means no per-row `message` event fires at all while a
folder's first sync is in progress, so an insert this listener ever sees
is live mail by construction, with no separate watermark needed.

CalendarIntakeHandler.decide() is a pure read: given a message and its
parsed invitation, it works out what *would* happen -- which calendar,
which existing object, which calendar_intake status -- without writing
anything. handle_message_event() is the only caller that turns a
decision into a write, and it does so gated by CalendarIntakeRepository's
own uniqueness on (account_id, msg_key): the calendar_intake row is
always written first, so a redelivered or resynced message finds the row
already there and applies nothing a second time. api/invitations.py's
GET reuses decide() for a message that never went through this listener
(backfilled mail, or a message that arrived before intake was wired up)
to render the same "here is what this invitation is" view without ever
writing calendar_intake itself -- only the explicit
POST .../import an api/invitations.py endpoint performs writes there
for that case, matching the design's "backfilled mail is never imported
automatically" -- automatically is the operative word, a person choosing
"add to calendar" is not automatic.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from mail_verdict.calendar import ical
from mail_verdict.database.models import Attachment, Identity, Message
from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.postimap.actions import create_object, replace_object_data

if TYPE_CHECKING:
    from mail_verdict.calendar.repository import (
        CalendarIntakeRepository,
        CalendarPrefsRepository,
        CollectionRepository,
        DavAccountRepository,
        DavObjectRepository,
    )
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.models import CalendarIntake, DavObject
    from mail_verdict.postimap.listener import PostimapEvent

logger = logging.getLogger(__name__)

# Matches ui/src/components/mail/reading-pane.tsx's CALENDAR_CONTENT_TYPES
# -- the two content types a mail client's own reading pane treats a
# message as carrying an invitation for, kept as the one place both sides
# agree on rather than restated.
CALENDAR_CONTENT_TYPES = ("text/calendar", "application/ics")

# calendar_intake.method's CHECK constraint also allows COUNTER, for a
# future counter-proposal flow -- nothing here handles it yet, so a
# COUNTER message is left alone rather than guessed at.
_HANDLED_METHODS = frozenset({"REQUEST", "CANCEL", "REPLY"})


@dataclass
class IntakeDecision:
    """What handle_message_event() would do with one invitation, and what
    api/invitations.py's GET renders for a message it has not (or not
    yet) been applied to. `existing` is the dav_objects row the UID
    already resolves to, when there is one -- present for update/cancel/
    reply/stale outcomes, absent for a fresh import or an unlinked one."""

    status: str
    reason: str | None = None
    dav_account_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None
    existing: DavObject | None = None
    identity: Identity | None = None


class CalendarIntakeHandler:
    """Decides what an invitation email means, and -- only from
    handle_message_event() -- applies it."""

    def __init__(
        self,
        db: DatabaseConnection,
        intake_repo: CalendarIntakeRepository,
        object_repo: DavObjectRepository,
        prefs_repo: CalendarPrefsRepository,
        collection_repo: CollectionRepository,
        dav_account_repo: DavAccountRepository,
    ) -> None:
        self._db = db
        self._intake_repo = intake_repo
        self._object_repo = object_repo
        self._prefs_repo = prefs_repo
        self._collection_repo = collection_repo
        self._dav_account_repo = dav_account_repo

    # --- the listener entry point ---

    async def handle_message_event(self, event: PostimapEvent) -> None:
        """Only a live arrival is ever acted on -- see the module
        docstring for why that needs no separate backfill check."""
        if event.type != "message" or event.op != "insert" or event.origin != "sync":
            return
        try:
            message_id = uuid.UUID(event.id)
            account_id = uuid.UUID(event.account_id)
        except ValueError:
            logger.warning("Invalid message insert payload: %s", event)
            return
        await self.process_arrival(message_id, account_id)

    async def process_arrival(self, message_id: uuid.UUID, account_id: uuid.UUID) -> None:
        message = await self._load_message(message_id, account_id)
        if message is None:
            return
        data = await self.find_calendar_attachment(message_id)
        if data is None:
            return
        try:
            invitation = ical.parse_itip_message(data)
        except ValueError:
            return
        if invitation.method not in _HANDLED_METHODS:
            return

        decision = await self.decide(account_id, message, invitation)
        row, created = await self._write_intake_row(account_id, message, invitation, decision)
        if not created:
            return
        await self._apply(row, decision, invitation, data)

    # --- the pure decision, shared with a GET that never writes ---

    async def decide(
        self, account_id: uuid.UUID, message: Message, invitation: ical.ParsedInvitation,
    ) -> IntakeDecision:
        """What this invitation resolves to, right now -- no writes."""
        existing = await self._object_repo.find_by_uid_reachable(invitation.master.uid, account_id)

        if invitation.method == "CANCEL":
            if existing is None:
                return IntakeDecision(status="ignored")
            if not self._organizer_authorized(existing, invitation):
                return IntakeDecision(
                    status="unauthorized", dav_account_id=existing.account_id,
                    collection_id=existing.collection_id, existing=existing,
                    reason="the sender does not match this event's organizer",
                )
            return IntakeDecision(
                status="cancelled", dav_account_id=existing.account_id,
                collection_id=existing.collection_id, existing=existing,
            )

        if invitation.method == "REPLY":
            if existing is None:
                return IntakeDecision(status="ignored")
            if not self._reply_attendee_authorized(message, invitation):
                return IntakeDecision(
                    status="unauthorized", dav_account_id=existing.account_id,
                    collection_id=existing.collection_id, existing=existing,
                    reason="the sender does not match the attendee this reply is for",
                )
            return IntakeDecision(
                status="updated", dav_account_id=existing.account_id,
                collection_id=existing.collection_id, existing=existing,
            )

        # REQUEST
        if existing is not None:
            if not self._organizer_authorized(existing, invitation):
                return IntakeDecision(
                    status="unauthorized", dav_account_id=existing.account_id,
                    collection_id=existing.collection_id, existing=existing,
                    reason="the sender does not match this event's organizer",
                )
            if self._is_stale(existing, invitation):
                return IntakeDecision(
                    status="ignored_stale", dav_account_id=existing.account_id,
                    collection_id=existing.collection_id, existing=existing,
                )
            return IntakeDecision(
                status="updated", dav_account_id=existing.account_id,
                collection_id=existing.collection_id, existing=existing,
            )

        identity = await self._resolve_identity(account_id, message, invitation)
        if identity is None:
            return IntakeDecision(status="unlinked", reason="no identity was among the attendees")

        collection_id = await self._prefs_repo.get_intake_collection_id(identity.id)
        if collection_id is None:
            return IntakeDecision(
                status="unlinked", identity=identity,
                reason=f"{identity.email} has no intake calendar",
            )
        collection = await self._collection_repo.get_by_id(collection_id)
        if collection is None or collection.deleted_at is not None:
            return IntakeDecision(
                status="unlinked", reason="the intake calendar no longer exists", identity=identity,
            )
        dav_account = await self._dav_account_repo.get_by_id(collection.account_id)
        if dav_account is None or not dav_account.is_active:
            return IntakeDecision(
                status="unlinked", identity=identity,
                reason="the calendar's DAV account is inactive",
            )
        return IntakeDecision(
            status="imported", dav_account_id=dav_account.id, collection_id=collection_id,
            identity=identity,
        )

    # --- applying a decision (listener only -- GET never calls this) ---

    async def _apply(
        self, row: CalendarIntake, decision: IntakeDecision,
        invitation: ical.ParsedInvitation, raw_data: str,
    ) -> None:
        if decision.status == "cancelled":
            assert decision.existing is not None
            new_data = ical.mark_cancelled(
                decision.existing.data, recurrence_id=invitation.master.recurrence_id,
            )
            async with self._db.session() as session:
                await replace_object_data(session, decision.existing.id, new_data)
            return

        if decision.status == "updated" and invitation.method == "REPLY":
            assert decision.existing is not None
            attendee = invitation.master.attendees[0] if invitation.master.attendees else None
            if attendee is None:
                return
            new_data = (
                ical.replace_exception_partstat_or_add(
                    decision.existing.data, attendee.email, attendee.partstat,
                    invitation.master.recurrence_id,
                )
                if invitation.master.recurrence_id is not None
                else ical.set_partstat(decision.existing.data, attendee.email, attendee.partstat)
            )
            async with self._db.session() as session:
                await replace_object_data(session, decision.existing.id, new_data)
            return

        if decision.status == "updated" and invitation.method == "REQUEST":
            assert decision.existing is not None
            fixed = ical.set_schedule_agent_client_on_organizer(ical.strip_method(raw_data))
            new_data = (
                ical.merge_exception(decision.existing.data, fixed, invitation.master.recurrence_id)
                if invitation.master.recurrence_id is not None
                else fixed
            )
            async with self._db.session() as session:
                await replace_object_data(session, decision.existing.id, new_data)
            return

        if decision.status == "imported":
            assert decision.dav_account_id is not None and decision.collection_id is not None
            fixed = ical.set_schedule_agent_client_on_organizer(ical.strip_method(raw_data))
            async with self._db.session() as session:
                obj = await create_object(
                    session, dav_account_id=decision.dav_account_id,
                    collection_id=decision.collection_id, data=fixed,
                )
            await self._intake_repo.update_status(row.id, status="imported", object_id=obj.id)
            return

        # ignored / ignored_stale / unlinked / unauthorized -- the intake
        # row already says everything there is to say; nothing to write
        # onto an object.

    # --- shared building blocks ---

    def _organizer_authorized(self, existing: DavObject, invitation: ical.ParsedInvitation) -> bool:
        """Whether this REQUEST/CANCEL is entitled to touch the object it
        names: the incoming ORGANIZER must match the object's own stored
        ORGANIZER, case-insensitively. Without this, anyone who merely
        knows an event's UID -- a co-attendee, since it is in the .ics
        they themselves received -- could silently rewrite or cancel it
        by emailing a REQUEST or CANCEL that names a UID they hold and an
        ORGANIZER of their own choosing; `_is_stale()` alone does not stop
        this, since SEQUENCE is attacker-controlled too."""
        try:
            master, _ = ical.parse_master_and_exceptions(existing.data)
        except ValueError:
            return False
        stored_organizer = master.organizer.email.lower() if master.organizer else None
        incoming_organizer = (
            invitation.master.organizer.email.lower() if invitation.master.organizer else None
        )
        if stored_organizer is None or incoming_organizer is None:
            return False
        return stored_organizer == incoming_organizer

    def _reply_attendee_authorized(
        self, message: Message, invitation: ical.ParsedInvitation,
    ) -> bool:
        """Whether this REPLY is entitled to write the PARTSTAT it
        carries: `_apply()` writes `attendees[0]`'s PARTSTAT onto the
        stored object without any other check, so a REPLY naming a UID
        the sender knows and an ATTENDEE of their own choosing could mark
        any other attendee's response -- accepted, declined, whatever the
        sender likes -- unless the attendee being updated is confirmed to
        be the message's own sender."""
        attendee = invitation.master.attendees[0] if invitation.master.attendees else None
        if attendee is None or not message.from_addr:
            return False
        return attendee.email.lower() == message.from_addr.lower()

    def _is_stale(self, existing: DavObject, invitation: ical.ParsedInvitation) -> bool:
        """SEQUENCE lower than the matching component's own stored
        SEQUENCE -- compared against the object's own resource, which is
        the source of truth for what has already been applied, not
        anything calendar_intake itself remembers."""
        try:
            master, exceptions = ical.parse_master_and_exceptions(existing.data)
        except ValueError:
            return False
        target = master
        if invitation.master.recurrence_id is not None:
            match = next(
                (e for e in exceptions if e.recurrence_id == invitation.master.recurrence_id), None,
            )
            if match is None:
                return False
            target = match
        return invitation.master.sequence < target.sequence

    async def _resolve_identity(
        self, account_id: uuid.UUID, message: Message, invitation: ical.ParsedInvitation,
    ) -> Identity | None:
        """ATTENDEE mailto: values intersected with this mail account's
        identities, falling back to to_addrs ∪ cc_addrs."""
        async with self._db.session() as session:
            result = await session.execute(
                select(Identity).where(Identity.account_id == account_id)
            )
            identities = result.scalars().all()
        if not identities:
            return None

        attendee_emails = {a.email.lower() for a in invitation.master.attendees}
        for identity in identities:
            if identity.email.lower() in attendee_emails:
                return identity

        fallback = {a.lower() for a in (message.to_addrs or [])}
        fallback |= {a.lower() for a in (message.cc_addrs or [])}
        for identity in identities:
            if identity.email.lower() in fallback:
                return identity
        return None

    async def resolve_attendee_identity(
        self, account_id: uuid.UUID, invitation: ical.ParsedInvitation,
    ) -> Identity | None:
        """The identity actually named in the ATTENDEE list -- never the
        to/cc fallback. api/invitations.py's own_address is this and
        nothing else: a forwarded invitation can still be imported, but
        is not something a reply can be sent for, which is exactly what
        own_address being null tells the UI."""
        async with self._db.session() as session:
            result = await session.execute(
                select(Identity).where(Identity.account_id == account_id)
            )
            identities = result.scalars().all()
        attendee_emails = {a.email.lower() for a in invitation.master.attendees}
        for identity in identities:
            if identity.email.lower() in attendee_emails:
                return identity
        return None

    async def find_calendar_attachment(self, message_id: uuid.UUID) -> str | None:
        """The first text/calendar or application/ics attachment's body,
        decoded -- None if the message carries none, or if it carries one
        with no bytes fetched yet (a truncated message)."""
        async with self._db.session() as session:
            result = await session.execute(
                select(Attachment).where(
                    Attachment.message_id == message_id,
                    Attachment.content_type.in_(CALENDAR_CONTENT_TYPES),
                )
            )
            attachment = result.scalars().first()
        if attachment is None or attachment.data is None:
            return None
        try:
            return attachment.data.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Calendar attachment on message %s is not UTF-8", message_id)
            return None

    async def _load_message(
        self, message_id: uuid.UUID, account_id: uuid.UUID,
    ) -> Message | None:
        async with self._db.session() as session:
            result = await session.execute(
                select(Message).where(
                    Message.id == message_id, Message.account_id == account_id,
                )
            )
            return result.scalar_one_or_none()

    async def _write_intake_row(
        self, account_id: uuid.UUID, message: Message,
        invitation: ical.ParsedInvitation, decision: IntakeDecision,
    ) -> tuple[CalendarIntake, bool]:
        msg_key = compute_msg_key(
            account_id=account_id, message_id_hdr=message.message_id,
            from_addr=message.from_addr, subject=message.subject,
            received_at=message.received_at, size_bytes=message.size_bytes,
        )
        return await self._intake_repo.create_if_absent(
            account_id=account_id, msg_key=msg_key, ical_uid=invitation.master.uid,
            method=invitation.method, sequence=invitation.master.sequence,
            recurrence_id=invitation.master.recurrence_id,
            dav_account_id=decision.dav_account_id, collection_id=decision.collection_id,
            object_id=decision.existing.id if decision.existing is not None else None,
            status=decision.status, reason=decision.reason,
        )
