"""
Invitation API endpoints -- the message-shaped view of calendar/intake.py.

GET  /api/calendar/invitations/:message_id          -- parsed invitation + intake status
POST /api/calendar/invitations/:message_id/import   -- manual import (or retry) into a calendar

Requires PostIMAP >= 1.6.0 -- see postimap/contract.py's MIN_DAV_SERVICE_VERSION.

GET never writes. For a message calendar/intake.py's listener already
processed, this reads the calendar_intake row it left; for one it never
saw (backfilled mail, or intake wired up after the message arrived), it
runs the same decide() the listener would have, without applying it --
"backfilled mail is never imported automatically" is about the listener,
not about a person looking at the message and choosing "add to
calendar", which is exactly what POST .../import is for.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from mail_verdict.api.schemas import (
    EventAttendeeOut,
    EventOrganizerOut,
    ImportInvitationRequest,
    InvitationResponse,
    OwnReplyOut,
)
from mail_verdict.calendar import ical
from mail_verdict.calendar.intake import CalendarIntakeHandler
from mail_verdict.calendar.repository import (
    CalendarIntakeRepository,
    CalendarPrefsRepository,
    CalendarReplyRepository,
    CollectionRepository,
    DavAccountRepository,
    DavObjectRepository,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Message, Outbox
from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.postimap.actions import create_object, replace_object_data
from mail_verdict.postimap.contract import read_postimap_info, supports_dav

router = APIRouter(prefix="/calendar/invitations", tags=["calendar"])

_UNSUPPORTED_DETAIL = (
    "Calendars and contacts require PostIMAP service_version >= 1.6.0; "
    "the running instance reports {version}."
)


async def _require_support() -> None:
    db = get_db_connection()
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_dav(info):
        raise HTTPException(
            status_code=501,
            detail=_UNSUPPORTED_DETAIL.format(version=info.service_version if info else "unknown"),
        )


def _handler() -> CalendarIntakeHandler:
    db = get_db_connection()
    return CalendarIntakeHandler(
        db, CalendarIntakeRepository(db), DavObjectRepository(db),
        CalendarPrefsRepository(db), CollectionRepository(db), DavAccountRepository(db),
    )


async def _load_message(message_id: uuid.UUID) -> Message:
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Message).where(Message.id == message_id))
        message = result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return message


async def _parsed_invitation(message: Message) -> tuple[str, ical.ParsedInvitation]:
    handler = _handler()
    data = await handler.find_calendar_attachment(message.id)
    if data is None:
        raise HTTPException(
            status_code=404, detail="Message carries no calendar invitation",
        )
    try:
        return data, ical.parse_itip_message(data)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Not a parseable calendar invitation: {exc}",
        ) from exc


async def _resolve_own_reply(
    reply_repo: CalendarReplyRepository, object_id: uuid.UUID, recurrence_id: str | None,
) -> OwnReplyOut | None:
    reply = await reply_repo.get_latest(object_id, recurrence_id)
    if reply is None:
        return None
    db = get_db_connection()
    async with db.session() as session:
        outbox_result = await session.execute(
            select(Outbox.status, Outbox.error).where(Outbox.id == reply.outbox_id)
        )
        row = outbox_result.one_or_none()
    return OwnReplyOut(
        partstat=reply.partstat,  # type: ignore[arg-type]
        outbox_id=reply.outbox_id,
        outbox_status=row.status if row else "pending",
        error=row.error if row else None,
        updated_at=reply.created_at,
    )


async def _calendar_name(collection_id: uuid.UUID | None) -> str | None:
    if collection_id is None:
        return None
    collection = await CollectionRepository(get_db_connection()).get_by_id(collection_id)
    if collection is None:
        return None
    return collection.display_name or collection.slug


async def _build_response(
    message: Message, invitation: ical.ParsedInvitation,
) -> InvitationResponse:
    db = get_db_connection()
    handler = _handler()
    intake_repo = CalendarIntakeRepository(db)
    object_repo = DavObjectRepository(db)
    reply_repo = CalendarReplyRepository(db)

    msg_key = compute_msg_key(
        account_id=message.account_id, message_id_hdr=message.message_id,
        from_addr=message.from_addr, subject=message.subject,
        received_at=message.received_at, size_bytes=message.size_bytes,
    )
    intake_row = await intake_repo.get_by_account_msg_key(message.account_id, msg_key)

    if intake_row is not None:
        status = intake_row.status
        collection_id = intake_row.collection_id
        object_id = intake_row.object_id
        sequence = (
            intake_row.sequence
            if intake_row.sequence is not None
            else invitation.master.sequence
        )
    else:
        decision = await handler.decide(message.account_id, message, invitation)
        if decision.existing is not None:
            # updated/cancelled/ignored_stale -- already true of the held
            # object regardless of whether a listener ever ran on this
            # message.
            status = decision.status
            collection_id = decision.collection_id
            object_id = decision.existing.id
        else:
            # decide()'s "imported" describes what an auto-import WOULD
            # do, not something that has actually happened -- nothing is
            # written here, so nothing has been imported yet either way.
            status = "ignored" if decision.status == "ignored" else "unlinked"
            collection_id = None
            object_id = None
        sequence = invitation.master.sequence

    error: str | None = None
    if object_id is not None:
        errors = await object_repo.get_unresolved_errors([object_id])
        found_error = errors.get(object_id)
        if found_error is not None:
            status = "failed"
            error = found_error

    attendee_identity = await handler.resolve_attendee_identity(message.account_id, invitation)

    own_reply = (
        await _resolve_own_reply(reply_repo, object_id, invitation.master.recurrence_id)
        if object_id is not None
        else None
    )

    return InvitationResponse(
        message_id=message.id,
        method=invitation.method,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        uid=invitation.master.uid,
        summary=invitation.master.summary,
        dtstart=invitation.master.dtstart,
        dtend=invitation.master.dtend,
        all_day=invitation.master.all_day,
        location=invitation.master.location,
        organizer=(
            EventOrganizerOut(
                email=invitation.master.organizer.email, cn=invitation.master.organizer.cn,
            )
            if invitation.master.organizer is not None
            else None
        ),
        attendees=[
            EventAttendeeOut(email=a.email, cn=a.cn, partstat=a.partstat, role=a.role)  # type: ignore[arg-type]
            for a in invitation.master.attendees
        ],
        own_address=attendee_identity.email if attendee_identity is not None else None,
        sequence=sequence,
        calendar_id=collection_id,
        calendar_name=await _calendar_name(collection_id),
        object_id=object_id,
        error=error,
        own_reply=own_reply,
    )


@router.get("/{message_id}", response_model=InvitationResponse)
async def get_invitation(message_id: uuid.UUID) -> InvitationResponse:
    """The parsed invitation, its intake status, and the calendar it
    ended up in (or would, or did before something went wrong)."""
    await _require_support()
    message = await _load_message(message_id)
    _data, invitation = await _parsed_invitation(message)
    return await _build_response(message, invitation)


@router.post("/{message_id}/import", response_model=InvitationResponse)
async def import_invitation(
    message_id: uuid.UUID, request: ImportInvitationRequest,
) -> InvitationResponse:
    """
    Import an invitation the listener left unlinked, or retry one that
    dead-lettered. If the UID already resolves to an object somewhere
    (this application's own earlier import, or one hand-created directly
    on the DAV server), that object is updated in place and calendar_id
    is not used to create a second copy -- the same invariant
    calendar/intake.py's own decide() enforces for a live arrival.
    """
    await _require_support()
    message = await _load_message(message_id)
    data, invitation = await _parsed_invitation(message)

    db = get_db_connection()
    collection_repo = CollectionRepository(db)
    collection = await collection_repo.get_by_id(request.calendar_id)
    if collection is None or collection.deleted_at is not None or collection.kind != "calendar":
        raise HTTPException(status_code=404, detail="Calendar not found")

    object_repo = DavObjectRepository(db)
    existing = await object_repo.find_by_uid_anywhere(invitation.master.uid)
    fixed = ical.set_schedule_agent_client_on_organizer(ical.strip_method(data))

    if existing is not None:
        target_collection = await collection_repo.get_by_id(existing.collection_id)
        if target_collection is not None and target_collection.read_only:
            raise HTTPException(status_code=400, detail="This calendar is read-only")
        new_data = (
            ical.merge_exception(existing.data, fixed, invitation.master.recurrence_id)
            if invitation.master.recurrence_id is not None
            else fixed
        )
        async with db.session() as session:
            await replace_object_data(session, existing.id, new_data)
        dav_account_id, target_collection_id, object_id = (
            existing.account_id, existing.collection_id, existing.id,
        )
        status = "updated"
    else:
        if collection.read_only:
            raise HTTPException(status_code=400, detail="This calendar is read-only")
        async with db.session() as session:
            obj = await create_object(
                session, dav_account_id=collection.account_id,
                collection_id=request.calendar_id, data=fixed,
            )
        dav_account_id, target_collection_id, object_id = (
            collection.account_id, request.calendar_id, obj.id,
        )
        status = "imported"

    intake_repo = CalendarIntakeRepository(db)
    msg_key = compute_msg_key(
        account_id=message.account_id, message_id_hdr=message.message_id,
        from_addr=message.from_addr, subject=message.subject,
        received_at=message.received_at, size_bytes=message.size_bytes,
    )
    intake_row = await intake_repo.get_by_account_msg_key(message.account_id, msg_key)
    if intake_row is None:
        await intake_repo.create_if_absent(
            account_id=message.account_id, msg_key=msg_key, ical_uid=invitation.master.uid,
            method=invitation.method, sequence=invitation.master.sequence,
            recurrence_id=invitation.master.recurrence_id,
            dav_account_id=dav_account_id, collection_id=target_collection_id,
            object_id=object_id, status=status, reason=None,
        )
    else:
        await intake_repo.update_status(
            intake_row.id, status=status, reason=None,
            dav_account_id=dav_account_id, collection_id=target_collection_id,
            object_id=object_id,
        )

    if request.link:
        handler = _handler()
        identity = await handler.resolve_attendee_identity(message.account_id, invitation)
        if identity is None:
            raise HTTPException(
                status_code=422,
                detail="No identity among the attendees to link this calendar to",
            )
        await CalendarPrefsRepository(db).update(
            target_collection_id, identity_id=identity.id, intake=True,
        )

    return await _build_response(message, invitation)
