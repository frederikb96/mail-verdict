"""
Calendar event API endpoints -- dav_objects of kind='calendar'.

GET    /api/calendar/events                  -- instances in a month, expanded
GET    /api/calendar/events/:id               -- one instance
POST   /api/calendar/events                   -- create
PATCH  /api/calendar/events/:id                -- edit (scope=this|following|all)
DELETE /api/calendar/events/:id                -- delete/cancel
POST   /api/calendar/events/:id/respond        -- RSVP

Requires PostIMAP >= 1.6.0 -- see postimap/contract.py's MIN_DAV_SERVICE_VERSION.

scope="following" (splitting a series into two, RFC 5546's
RANGE=THISANDFUTURE) is not implemented -- rejected with 422 rather than
silently treated as scope="this", per the design's own rule that an
unsupported scope must never be accepted and ignored.

POST honours tz -- an IANA zone name (e.g. "Europe/Berlin") DTSTART/DTEND
are bound to, so the stored event carries a named-zone TZID rather than
only a fixed UTC offset. PATCH rejects both attendees and tz outright
(422) rather than accept and silently drop them -- neither is applied
by update_event. Changing who is invited on an already-sent invitation
needs its own REQUEST/CANCEL sends, the way create_event/delete_event
already give attendees; re-timezoning an existing event without also
moving dtstart/dtend has no settled meaning here, unlike a fresh create,
where there is no existing wall-clock reading to reconcile against. An
edit keeps whatever zone the event is already bound to and writes its new
instants against that, so a series does not need tz to stay correct
across a daylight-saving change.

source_message_id (which invitation email an event came from) is left
null throughout: resolving it needs a join from calendar_intake.msg_key
back to messages.message_id, which only works for the common case where
msg_key is the literal header rather than its hash fallback, and is not
implemented here -- flagged in the report as unfinished.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from mail_verdict.api.schemas import (
    EventAttendeeOut,
    EventCreateRequest,
    EventDeleteRequest,
    EventInstanceOut,
    EventListResponse,
    EventOrganizerOut,
    EventUpdateRequest,
    OwnReplyOut,
    RespondRequest,
)
from mail_verdict.calendar import ical
from mail_verdict.calendar.repository import (
    CalendarPrefsRepository,
    CalendarReplyRepository,
    CollectionRepository,
    DavObjectRepository,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import CalendarPrefs, DavCollection, DavObject, Identity, Outbox
from mail_verdict.postimap.actions import (
    create_object,
    delete_object,
    insert_outbox,
    move_object,
    replace_object_data,
)
from mail_verdict.postimap.contract import read_postimap_info, supports_dav

router = APIRouter(prefix="/calendar/events", tags=["calendar"])

logger = logging.getLogger(__name__)

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


def _parse_month(month: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"month must be YYYY-MM, got {month!r}",
        ) from exc
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


async def resolve_own_reply(
    reply_repo: CalendarReplyRepository, object_id: uuid.UUID, recurrence_id: str | None,
) -> OwnReplyOut | None:
    """
    The identity's own RSVP reply for this event, if it has replied at
    all -- shared with api/invitations.py's own invitation-detail view,
    which needs the identical answer to the identical question rather
    than a second copy of it.

    outbox_status reads the outbox row's live status while it still
    exists. Once retention has purged that row, "unknown" is reported --
    never "pending", which is the status an active, in-flight send uses
    and would otherwise claim the reply is still sending forever: a row
    genuinely still pending can become "sent" or "failed" on its own, but
    a row that is simply gone never will, so reusing "pending" for it is
    a claim that can never stop being wrong once made.
    """
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
        outbox_status=row.status if row else "unknown",
        error=row.error if row else None,
        updated_at=reply.created_at,
    )


async def _to_instance(
    parsed: ical.ParsedEvent, obj: DavObject, *,
    own_identity_email: str | None, read_only: bool, sync_error: str | None,
    reply_repo: CalendarReplyRepository,
) -> EventInstanceOut:
    own_email = own_identity_email.lower() if own_identity_email else None
    own_attendee = next(
        (a for a in parsed.attendees if own_email and a.email.lower() == own_email), None,
    )
    own_reply = None
    if own_attendee is not None:
        own_reply = await resolve_own_reply(reply_repo, obj.id, parsed.recurrence_id)

    return EventInstanceOut(
        object_id=obj.id,
        recurrence_id=parsed.recurrence_id,
        calendar_id=obj.collection_id,
        uid=parsed.uid,
        summary=parsed.summary,
        dtstart=parsed.dtstart,
        dtend=parsed.dtend,
        tz=parsed.tz,
        all_day=parsed.all_day,
        location=parsed.location,
        description=parsed.description,
        status=parsed.status,  # type: ignore[arg-type]
        sequence=parsed.sequence,
        rrule=parsed.rrule,
        organizer=(
            EventOrganizerOut(email=parsed.organizer.email, cn=parsed.organizer.cn)
            if parsed.organizer else None
        ),
        attendees=[
            EventAttendeeOut(email=a.email, cn=a.cn, partstat=a.partstat, role=a.role)  # type: ignore[arg-type]
            for a in parsed.attendees
        ],
        partstat=own_attendee.partstat if own_attendee else None,  # type: ignore[arg-type]
        is_recurring=parsed.is_recurring,
        is_exception=parsed.is_exception,
        pending=obj.etag is None,
        sync_error=sync_error,
        own_reply=own_reply,
        source_message_id=None,
        read_only=read_only,
    )


# Expanding recurrence has no await point of its own -- it is icalendar
# parsing plus dateutil's RRULE walk, both plain CPU, and a month view
# can carry one such object per visible calendar. Run the whole batch
# once on a worker thread rather than the request's own coroutine, so it
# cannot hold up every other request sharing this process's one event
# loop while it works. The budget below is the backstop for a single
# object recurring-ical-events cannot expand quickly despite that: a
# thread has no cooperative way to be interrupted mid-walk, so timing out
# abandons the wait rather than the underlying computation -- that
# object's occurrences are simply missing from the response, the same
# best-effort contract this view already keeps for a parse failure.
#
# A thread that outlives its own timeout keeps occupying whatever pool it
# was submitted to until it eventually finishes on its own -- a
# pathological object retried on every request permanently strands one
# more worker. A dedicated, bounded pool contains that to calendar
# expansion alone, rather than eventually starving every unrelated
# asyncio.to_thread() call sharing the loop's own default executor.
_EXPANSION_TIMEOUT_SECONDS = 10.0
_EXPANSION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="calendar-expand",
)


def _expand_all_sync(
    objects: list[DavObject], window_start: datetime, window_end: datetime,
) -> dict[uuid.UUID, list[ical.ParsedEvent]]:
    expanded: dict[uuid.UUID, list[ical.ParsedEvent]] = {}
    for obj in objects:
        try:
            expanded[obj.id] = ical.expand_instances(obj.data, window_start, window_end)
        except Exception:
            # A single malformed or pathological object (an unparseable
            # body, or one that would expand past the occurrence bound)
            # must never take the whole month view down with it -- catch
            # broadly rather than ValueError alone, since a library-level
            # parse failure is not guaranteed to be one.
            logger.warning(
                "Skipping calendar object %s in month view", obj.id, exc_info=True,
            )
    return expanded


async def _expand_all(
    objects: list[DavObject], window_start: datetime, window_end: datetime,
) -> dict[uuid.UUID, list[ical.ParsedEvent]]:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                _EXPANSION_EXECUTOR, _expand_all_sync, objects, window_start, window_end,
            ),
            timeout=_EXPANSION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Calendar expansion exceeded %.0fs for %d objects; returning none of them",
            _EXPANSION_TIMEOUT_SECONDS, len(objects),
        )
        return {}


@router.get("", response_model=EventListResponse)
async def list_events(month: str, calendars: str | None = None) -> EventListResponse:
    """Every instance (recurring series expanded) in one calendar-month
    window, across every visible calendar unless `calendars` narrows it."""
    await _require_support()
    window_start, window_end = _parse_month(month)

    db = get_db_connection()
    collection_repo = CollectionRepository(db)
    prefs_repo = CalendarPrefsRepository(db)
    object_repo = DavObjectRepository(db)
    reply_repo = CalendarReplyRepository(db)

    pairs = await collection_repo.list_by_kind("calendar")
    all_prefs = await prefs_repo.list_all()

    requested_ids = {uuid.UUID(c) for c in calendars.split(",") if c} if calendars else None
    candidates: list[tuple[DavCollection, CalendarPrefs | None]] = []
    for collection, _account in pairs:
        if requested_ids is not None and collection.id not in requested_ids:
            continue
        prefs = all_prefs.get(collection.id)
        # is_enabled gates whether the calendar is offered at all (the
        # sidebar list, the event editor's picker); is_visible is the
        # separate per-view toggle over an offered calendar. Either one
        # off means this calendar's events are absent from the month
        # view -- an explicit `calendars` param (opened directly, e.g.
        # from the editor's own calendar field) still bypasses both, the
        # same way it already bypassed is_visible.
        hidden = prefs is not None and (not prefs.is_visible or not prefs.is_enabled)
        if requested_ids is None and hidden:
            continue
        candidates.append((collection, prefs))

    # One query for every identity a visible calendar is linked to,
    # instead of one per calendar -- list_by_kind already returned every
    # collection there is, so this was a request-shaped fan-out over a
    # fixed, small set that a single IN() covers just as well.
    identity_ids = {
        prefs.identity_id for _, prefs in candidates
        if prefs is not None and prefs.identity_id is not None
    }
    identity_emails: dict[uuid.UUID, str] = {}
    if identity_ids:
        async with db.session() as session:
            result = await session.execute(
                select(Identity.id, Identity.email).where(Identity.id.in_(identity_ids))
            )
            identity_emails = dict(result.tuples().all())

    visible: list[tuple[DavCollection, str | None]] = [
        (
            collection,
            identity_emails.get(prefs.identity_id)
            if prefs is not None and prefs.identity_id is not None else None,
        )
        for collection, prefs in candidates
    ]

    collection_ids = [c.id for c, _ in visible]
    objects = await object_repo.list_in_collections(collection_ids, window_start, window_end)
    errors = await object_repo.get_write_errors([o.id for o in objects])
    identity_by_collection = {c.id: email for c, email in visible}
    read_only_by_collection = {c.id: c.read_only for c, _ in visible}

    expanded = await _expand_all(objects, window_start, window_end)

    events: list[EventInstanceOut] = []
    for obj in objects:
        instances = expanded.get(obj.id)
        if instances is None:
            continue
        for parsed in instances:
            events.append(
                await _to_instance(
                    parsed, obj,
                    own_identity_email=identity_by_collection.get(obj.collection_id),
                    read_only=read_only_by_collection.get(obj.collection_id, False),
                    sync_error=errors.get(obj.id),
                    reply_repo=reply_repo,
                )
            )
    events.sort(key=lambda e: e.dtstart)
    return EventListResponse(events=events)


@router.get("/{object_id}", response_model=EventInstanceOut)
async def get_event(object_id: uuid.UUID, recurrence_id: str | None = None) -> EventInstanceOut:
    """One event instance -- the master, or one named occurrence."""
    await _require_support()
    db = get_db_connection()
    object_repo = DavObjectRepository(db)
    obj = await object_repo.get_by_id(object_id)
    if obj is None or obj.deleted_at is not None or obj.kind != "calendar":
        raise HTTPException(status_code=404, detail="Event not found")

    master, exceptions = ical.parse_master_and_exceptions(obj.data)
    parsed: ical.ParsedEvent | None = master
    if recurrence_id is not None:
        parsed = next((e for e in exceptions if e.recurrence_id == recurrence_id), None)
        if parsed is None:
            # No stored exception at this occurrence -- RECURRENCE-ID is
            # itself the occurrence's own DTSTART (RFC 5545), so a
            # one-day window around that exact moment is where it would
            # be, without expanding the whole series to look for it.
            try:
                target = ical.recurrence_id_to_datetime(recurrence_id)
                candidates = ical.expand_instances(
                    obj.data, target, target + timedelta(days=1),
                )
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    status_code=404, detail="Occurrence not found",
                ) from exc
            parsed = next((c for c in candidates if c.recurrence_id == recurrence_id), None)
        if parsed is None:
            raise HTTPException(status_code=404, detail="Occurrence not found")
    assert parsed is not None

    collection_repo = CollectionRepository(db)
    collection = await collection_repo.get_by_id(obj.collection_id)
    prefs = await CalendarPrefsRepository(db).get(obj.collection_id)
    identity_email = None
    if prefs is not None and prefs.identity_id is not None:
        async with db.session() as session:
            identity_email = await session.scalar(
                select(Identity.email).where(Identity.id == prefs.identity_id)
            )
    errors = await object_repo.get_write_errors([obj.id])
    return await _to_instance(
        parsed, obj, own_identity_email=identity_email,
        read_only=collection.read_only if collection else False,
        sync_error=errors.get(obj.id), reply_repo=CalendarReplyRepository(db),
    )


@router.post("", response_model=EventInstanceOut, status_code=201)
async def create_event(request: EventCreateRequest) -> EventInstanceOut:
    """Create an event. With attendees, the calendar needs a linked
    identity -- MailVerdict sends the invitations itself over its outbox
    rather than leaving the server's own scheduling engine to do it."""
    await _require_support()
    db = get_db_connection()
    collection_repo = CollectionRepository(db)
    collection = await collection_repo.get_by_id(request.calendar_id)
    if collection is None or collection.kind != "calendar":
        raise HTTPException(status_code=404, detail="Calendar not found")

    organizer_email = None
    organizer_identity: Identity | None = None
    if request.attendees:
        prefs = await CalendarPrefsRepository(db).get(request.calendar_id)
        if prefs is None or prefs.identity_id is None:
            raise HTTPException(status_code=409, detail="calendar has no identity")
        async with db.session() as session:
            organizer_identity = await session.scalar(
                select(Identity).where(Identity.id == prefs.identity_id)
            )
        if organizer_identity is None:
            raise HTTPException(status_code=409, detail="calendar has no identity")
        organizer_email = organizer_identity.email

    try:
        data = ical.build_new_event(
            summary=request.summary, dtstart=request.dtstart, dtend=request.dtend,
            all_day=request.all_day, location=request.location, description=request.description,
            rrule=request.rrule, tz=request.tz, organizer_email=organizer_email,
            organizer_cn=organizer_identity.display_name if organizer_identity else None,
            attendees=[(a.email, a.cn) for a in request.attendees] if request.attendees else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with db.session() as session:
        obj = await create_object(
            session, dav_account_id=collection.account_id,
            collection_id=request.calendar_id, data=data,
        )

    if request.attendees and organizer_identity is not None:
        await _send_itip(
            organizer_identity, data, method="REQUEST",
            to_addrs=[a.email for a in request.attendees],
            subject=f"Invitation: {request.summary}",
        )

    master, _ = ical.parse_master_and_exceptions(data)
    return await _to_instance(
        master, obj, own_identity_email=organizer_email, read_only=False, sync_error=None,
        reply_repo=CalendarReplyRepository(db),
    )


async def _send_itip(
    identity: Identity, event_data: str, *, method: str, to_addrs: list[str], subject: str,
) -> None:
    """Send an .ics attachment over the identity's own outbox -- never a
    direct SMTP call, matching how this application sends everything
    else."""
    mailed_ics = ical.with_method(event_data, method)
    content_type = f"text/calendar; method={method}; charset=utf-8"
    db = get_db_connection()
    async with db.session() as session:
        await insert_outbox(
            session, account_id=identity.account_id, kind="send", from_addr=identity.email,
            to_addrs=to_addrs, subject=subject,
            body_text=f"{subject}\n\nThis message contains a calendar invitation.",
            attachments=[("invite.ics", content_type, mailed_ics.encode())],
        )


@router.patch("/{object_id}", response_model=EventInstanceOut)
async def update_event(object_id: uuid.UUID, request: EventUpdateRequest) -> EventInstanceOut:
    """Edit an event. scope="all" edits the whole series (or the only edit
    path for a non-recurring event); scope="this" edits one occurrence
    without touching the others; scope="following" is not implemented.
    attendees and tz are refused outright (422) rather than accepted and
    silently dropped -- neither is applied by this endpoint."""
    await _require_support()
    if request.scope == "following":
        raise HTTPException(
            status_code=422, detail="scope=following (splitting a series) is not implemented",
        )
    # Neither field is applied below -- changing who is invited needs its
    # own REQUEST/CANCEL sends the way create_event/delete_event already
    # give attendees, which this endpoint does not do, and tz has no
    # settled meaning apart from dtstart/dtend, which already carry their
    # own instant. Rejecting outright is the one option that rules out a
    # write reporting success for a field it never touched.
    if request.attendees is not None:
        raise HTTPException(
            status_code=422, detail="changing attendees on an existing event is not supported",
        )
    if request.tz is not None:
        raise HTTPException(
            status_code=422,
            detail="tz cannot be set on an existing event; dtstart/dtend already carry the instant",
        )

    db = get_db_connection()
    object_repo = DavObjectRepository(db)
    obj = await object_repo.get_by_id(object_id)
    if obj is None or obj.deleted_at is not None or obj.kind != "calendar":
        raise HTTPException(status_code=404, detail="Event not found")

    collection_repo = CollectionRepository(db)
    collection = await collection_repo.get_by_id(obj.collection_id)
    if collection is not None and collection.read_only:
        raise HTTPException(status_code=400, detail="This calendar is read-only")

    if request.recurrence_id is not None and request.scope != "this":
        raise HTTPException(
            status_code=400, detail="recurrence_id requires scope=this",
        )

    current_master, _ = ical.parse_master_and_exceptions(obj.data)
    prefs = await CalendarPrefsRepository(db).get(obj.collection_id)
    identity_email = None
    organizer_identity: Identity | None = None
    if prefs is not None and prefs.identity_id is not None:
        async with db.session() as session:
            identity = await session.scalar(
                select(Identity).where(Identity.id == prefs.identity_id)
            )
        if identity is not None:
            identity_email = identity.email
            if current_master.organizer and identity.email.lower() == (
                current_master.organizer.email.lower()
            ):
                organizer_identity = identity

    # SEQUENCE is the ORGANIZER's own version counter (RFC 5545) --
    # bumping it on an edit to an event this calendar does not organize
    # makes the real organizer's next genuine update look stale by
    # comparison to calendar/intake.py's own staleness check, and lose
    # silently. A purely local event (no ORGANIZER at all -- nothing
    # created it as an invitation) has no such external version to
    # collide with, so it keeps advancing as before; only an event
    # organized by someone else is held back.
    bump_sequence = current_master.organizer is None or organizer_identity is not None

    if request.scope == "this":
        if request.recurrence_id is None:
            raise HTTPException(status_code=400, detail="scope=this requires recurrence_id")
        try:
            updated_data = ical.edit_occurrence(
                obj.data, request.recurrence_id,
                summary=request.summary, dtstart=request.dtstart, dtend=request.dtend,
                all_day=request.all_day, location=request.location,
                description=request.description, bump_sequence=bump_sequence,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        try:
            updated_data = ical.replace_master_fields(
                obj.data,
                summary=request.summary, dtstart=request.dtstart, dtend=request.dtend,
                all_day=request.all_day, location=request.location,
                description=request.description, rrule=request.rrule,
                bump_sequence=bump_sequence,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with db.session() as session:
        await replace_object_data(session, object_id, updated_data)
        if request.calendar_id is not None and request.calendar_id != obj.collection_id:
            await move_object(session, object_id, request.calendar_id)

    refreshed = await object_repo.get_by_id(object_id)
    assert refreshed is not None
    master, exceptions = ical.parse_master_and_exceptions(refreshed.data)
    parsed = master
    if request.recurrence_id is not None:
        parsed = next(
            (e for e in exceptions if e.recurrence_id == request.recurrence_id), master,
        )

    # Create sends a REQUEST and delete sends a CANCEL -- an edit that
    # sent nothing taught the wrong lesson: an attendee's calendar still
    # says the old time, with nothing telling them it moved. Gated
    # exactly as delete_event gates its CANCEL, below.
    if organizer_identity is not None and master.attendees:
        await _send_itip(
            organizer_identity, refreshed.data, method="REQUEST",
            to_addrs=[a.email for a in master.attendees],
            subject=f"Updated: {master.summary}",
        )

    return await _to_instance(
        parsed, refreshed, own_identity_email=identity_email,
        read_only=collection.read_only if collection else False, sync_error=None,
        reply_repo=CalendarReplyRepository(db),
    )


@router.delete("/{object_id}", status_code=204)
async def delete_event(object_id: uuid.UUID, request: EventDeleteRequest | None = None) -> None:
    """
    Delete an event. scope="all" (or no scope, non-recurring) removes it
    from the calendar; scope="this" marks that one occurrence cancelled
    instead, since the event still exists for every other occurrence.
    An event this identity organizes with attendees sends CANCEL first.
    """
    await _require_support()
    scope = request.scope if request else None
    recurrence_id = request.recurrence_id if request else None
    if scope == "following":
        raise HTTPException(
            status_code=422, detail="scope=following (splitting a series) is not implemented",
        )

    db = get_db_connection()
    object_repo = DavObjectRepository(db)
    obj = await object_repo.get_by_id(object_id)
    if obj is None or obj.deleted_at is not None or obj.kind != "calendar":
        raise HTTPException(status_code=404, detail="Event not found")

    master, _ = ical.parse_master_and_exceptions(obj.data)
    prefs = await CalendarPrefsRepository(db).get(obj.collection_id)
    organizer_identity: Identity | None = None
    if prefs is not None and prefs.identity_id is not None and master.organizer:
        async with db.session() as session:
            organizer_identity = await session.scalar(
                select(Identity).where(
                    Identity.id == prefs.identity_id,
                    Identity.email.ilike(master.organizer.email),
                )
            )

    if scope == "this":
        if recurrence_id is None:
            raise HTTPException(status_code=400, detail="scope=this requires recurrence_id")
        # See update_event's own comment: SEQUENCE only advances for a
        # purely local event or one this calendar organizes, or the
        # organizer's next genuine update to this occurrence looks stale
        # by comparison.
        bump_sequence = master.organizer is None or organizer_identity is not None
        updated_data = ical.mark_cancelled(
            obj.data, recurrence_id=recurrence_id, bump_sequence=bump_sequence,
        )
        async with db.session() as session:
            await replace_object_data(session, object_id, updated_data)
        if organizer_identity is not None and master.attendees:
            await _send_itip(
                organizer_identity, updated_data, method="CANCEL",
                to_addrs=[a.email for a in master.attendees],
                subject=f"Cancelled: {master.summary}",
            )
        return

    if organizer_identity is not None and master.attendees:
        cancelled_data = ical.mark_cancelled(obj.data)
        await _send_itip(
            organizer_identity, cancelled_data, method="CANCEL",
            to_addrs=[a.email for a in master.attendees],
            subject=f"Cancelled: {master.summary}",
        )

    async with db.session() as session:
        await delete_object(session, object_id)


@router.post("/{object_id}/respond", response_model=EventInstanceOut)
async def respond_to_event(object_id: uuid.UUID, request: RespondRequest) -> EventInstanceOut:
    """
    Accept, decline or tentatively accept an invitation: updates PARTSTAT
    on the stored object immediately, and sends the REPLY over the
    identity's own outbox. A failing send does not roll the PARTSTAT
    back -- calling respond again inserts a fresh outbox row, matching
    how a failed send already behaves everywhere else in this
    application.
    """
    await _require_support()
    db = get_db_connection()
    object_repo = DavObjectRepository(db)
    obj = await object_repo.get_by_id(object_id)
    if obj is None or obj.deleted_at is not None or obj.kind != "calendar":
        raise HTTPException(status_code=404, detail="Event not found")

    async with db.session() as session:
        identity = await session.scalar(select(Identity).where(Identity.id == request.identity_id))
    if identity is None:
        raise HTTPException(status_code=404, detail="Identity not found")

    master, exceptions = ical.parse_master_and_exceptions(obj.data)
    target = master
    if request.recurrence_id is not None:
        target = next(
            (e for e in exceptions if e.recurrence_id == request.recurrence_id), master,
        )
    attendee_emails = {a.email.lower() for a in target.attendees}
    if identity.email.lower() not in attendee_emails:
        raise HTTPException(
            status_code=409,
            detail=f"{identity.email} is not an attendee of this event",
        )

    if request.recurrence_id is not None:
        updated_data = ical.replace_exception_partstat_or_add(
            obj.data, identity.email, request.partstat, request.recurrence_id,
        )
    else:
        updated_data = ical.set_partstat(obj.data, identity.email, request.partstat)

    async with db.session() as session:
        await replace_object_data(session, object_id, updated_data)

    reply_ics = ical.build_reply_ics(
        updated_data, attendee_email=identity.email, partstat=request.partstat,
        comment=request.comment, recurrence_id=request.recurrence_id,
    )
    organizer_email = target.organizer.email if target.organizer else None
    if organizer_email:
        reply_type = "text/calendar; method=REPLY; charset=utf-8"
        attachment = ("invite.ics", reply_type, reply_ics.encode())
        async with db.session() as session:
            outbox = await insert_outbox(
                session, account_id=identity.account_id, kind="send", from_addr=identity.email,
                to_addrs=[organizer_email],
                subject=f"{request.partstat.capitalize()}: {target.summary}",
                body_text=f"{identity.email} has {request.partstat} the invitation.",
                attachments=[attachment],
            )
        await CalendarReplyRepository(db).create(
            object_id=object_id, recurrence_id=request.recurrence_id,
            identity_id=identity.id, partstat=request.partstat, outbox_id=outbox.id,
        )

    refreshed = await object_repo.get_by_id(object_id)
    assert refreshed is not None
    master2, exceptions2 = ical.parse_master_and_exceptions(refreshed.data)
    parsed = master2
    if request.recurrence_id is not None:
        parsed = next(
            (e for e in exceptions2 if e.recurrence_id == request.recurrence_id), master2,
        )
    collection_repo = CollectionRepository(db)
    collection = await collection_repo.get_by_id(refreshed.collection_id)
    return await _to_instance(
        parsed, refreshed, own_identity_email=identity.email,
        read_only=collection.read_only if collection else False, sync_error=None,
        reply_repo=CalendarReplyRepository(db),
    )
