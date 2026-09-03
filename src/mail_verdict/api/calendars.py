"""
Calendar API endpoints -- dav_collections of kind='calendar' merged with
calendar_prefs, plus the identity-to-calendar mapping document.

GET    /api/calendars           -- every visible calendar, prefs merged in
POST   /api/calendars           -- create one on the server
PATCH  /api/calendars/:id       -- rename, recolour, link an identity, set intake
DELETE /api/calendars/:id       -- destroy it and every event in it, irreversibly
GET    /api/calendar/links      -- the whole identity-to-calendar mapping
PUT    /api/calendar/links      -- replace it, optimistic on base_revision
GET    /api/addressbooks        -- every visible address book
POST   /api/addressbooks        -- create one on the server

Requires PostIMAP >= 1.6.0 -- see postimap/contract.py's MIN_DAV_SERVICE_VERSION.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from mail_verdict.api.schemas import (
    AddressbookCreateRequest,
    AddressbookSummaryResponse,
    CalendarCreateRequest,
    CalendarIntakeState,
    CalendarLinkRowOut,
    CalendarLinksOut,
    CalendarLinksUpdateRequest,
    CalendarResponse,
    CalendarUpdateRequest,
)
from mail_verdict.calendar.repository import (
    CalendarLinksRevisionRepository,
    CalendarPrefsRepository,
    CollectionRepository,
    DavAccountRepository,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import CalendarPrefs, DavAccount, DavCollection, Identity
from mail_verdict.postimap.actions import create_collection, delete_collection, update_collection
from mail_verdict.postimap.contract import read_postimap_info, supports_dav

router = APIRouter(prefix="/calendars", tags=["calendars"])
links_router = APIRouter(prefix="/calendar/links", tags=["calendars"])
addressbooks_router = APIRouter(prefix="/addressbooks", tags=["contacts"])

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


def _slugify(display_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


def _intake_state(prefs: CalendarPrefs | None) -> CalendarIntakeState:
    """`intake` is a database bool; the wire type is a three-value enum.
    This never emits "import" -- calendar_prefs has no state between
    "not the intake calendar" and "the intake calendar", and the "import
    without linking" idea the UI's third value implies is already covered
    at the per-message level by ImportInvitationRequest.link. See the
    report to main for where this needs a deliberate decision instead."""
    if prefs is not None and prefs.intake and prefs.identity_id is not None:
        return "import_and_link"
    return "none"


def _to_response(
    collection: DavCollection, account: DavAccount, prefs: CalendarPrefs | None,
) -> CalendarResponse:
    return CalendarResponse(
        id=collection.id,
        dav_account_id=account.id,
        dav_account_name=account.name,
        display_name=collection.display_name or collection.slug,
        color=collection.color or "",
        color_override=prefs.color_override if prefs else None,
        is_visible=prefs.is_visible if prefs else True,
        read_only=collection.read_only,
        identity_id=prefs.identity_id if prefs else None,
        intake=_intake_state(prefs),
        supported_components=list(collection.supported_components or []),
        sync_error=collection.sync_error,
        initial_sync_done=collection.initial_sync_done,
        total_count=collection.total_count,
    )


@router.get("", response_model=list[CalendarResponse])
async def list_calendars() -> list[CalendarResponse]:
    """Every non-deleted calendar, prefs merged in."""
    await _require_support()
    db = get_db_connection()
    collection_repo = CollectionRepository(db)
    prefs_repo = CalendarPrefsRepository(db)
    pairs = await collection_repo.list_by_kind("calendar")
    all_prefs = await prefs_repo.list_all()
    return [_to_response(c, a, all_prefs.get(c.id)) for c, a in pairs]


@router.post("", response_model=CalendarResponse, status_code=201)
async def create_calendar(request: CalendarCreateRequest) -> CalendarResponse:
    """Create a calendar on the server. PostIMAP issues MKCALENDAR and
    writes href back once it lands."""
    await _require_support()
    db = get_db_connection()
    account_repo = DavAccountRepository(db)
    account = await account_repo.get_by_id(request.dav_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="DAV account not found")

    async with db.session() as session:
        collection = await create_collection(
            session, dav_account_id=request.dav_account_id, kind="calendar",
            slug=_slugify(request.display_name), display_name=request.display_name,
            color=request.color,
        )
    return _to_response(collection, account, None)


@router.patch("/{collection_id}", response_model=CalendarResponse)
async def update_calendar(
    collection_id: uuid.UUID, request: CalendarUpdateRequest,
) -> CalendarResponse:
    """Rename/recolour on the server, or change the local prefs (colour
    override, visibility, identity link, intake)."""
    await _require_support()
    db = get_db_connection()
    collection_repo = CollectionRepository(db)
    collection = await collection_repo.get_by_id(collection_id)
    if collection is None or collection.deleted_at is not None or collection.kind != "calendar":
        raise HTTPException(status_code=404, detail="Calendar not found")

    values = request.model_dump(exclude_unset=True)

    if "display_name" in values:
        async with db.session() as session:
            await update_collection(session, collection_id, display_name=values["display_name"])

    prefs_repo = CalendarPrefsRepository(db)
    prefs_fields: dict[str, object] = {}
    if "color_override" in values:
        prefs_fields["color_override"] = values["color_override"]
    if "is_visible" in values:
        prefs_fields["is_visible"] = values["is_visible"]
    if "identity_id" in values:
        prefs_fields["identity_id"] = values["identity_id"]
    if "intake" in values:
        wants_intake = values["intake"] in ("import", "import_and_link")
        prefs_fields["intake"] = wants_intake
        if wants_intake and "identity_id" not in values:
            existing_prefs = await prefs_repo.get(collection_id)
            if existing_prefs is None or existing_prefs.identity_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="A calendar needs an identity before it can receive invitations",
                )

    prefs: CalendarPrefs | None
    if prefs_fields:
        prefs = await prefs_repo.update(collection_id, **prefs_fields)
    else:
        prefs = await prefs_repo.get(collection_id)

    account_repo = DavAccountRepository(db)
    account = await account_repo.get_by_id(collection.account_id)
    assert account is not None
    collection = await collection_repo.get_by_id(collection_id)
    assert collection is not None
    return _to_response(collection, account, prefs)


@router.delete("/{collection_id}", status_code=204)
async def delete_calendar(
    collection_id: uuid.UUID,
    confirm_event_count: int | None = Query(
        default=None,
        description=(
            "Required to actually delete. Omit it (or get it wrong) and the "
            "request fails with a 409 naming the calendar's current event "
            "count; repeat the call with that number to confirm."
        ),
    ),
) -> None:
    """Delete a calendar -- destroys every event in it on the server,
    irreversibly. There is no undo."""
    await _require_support()
    db = get_db_connection()
    collection_repo = CollectionRepository(db)
    collection = await collection_repo.get_by_id(collection_id)
    if collection is None or collection.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Calendar not found")

    if confirm_event_count != collection.total_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This calendar holds {collection.total_count} event(s), all destroyed "
                "on the server irreversibly if deleted. Repeat the request with "
                f"?confirm_event_count={collection.total_count} to proceed."
            ),
        )

    async with db.session() as session:
        await delete_collection(session, collection_id)


@addressbooks_router.get("", response_model=list[AddressbookSummaryResponse])
async def list_addressbooks() -> list[AddressbookSummaryResponse]:
    """Every non-deleted address book."""
    await _require_support()
    collection_repo = CollectionRepository(get_db_connection())
    pairs = await collection_repo.list_by_kind("addressbook")
    return [
        AddressbookSummaryResponse(
            id=c.id, dav_account_id=a.id, dav_account_name=a.name,
            display_name=c.display_name or c.slug, read_only=c.read_only,
            total_count=c.total_count,
        )
        for c, a in pairs
    ]


@addressbooks_router.post("", response_model=AddressbookSummaryResponse, status_code=201)
async def create_addressbook(request: AddressbookCreateRequest) -> AddressbookSummaryResponse:
    """Create an address book on the server. PostIMAP issues MKCOL and
    writes href back once it lands."""
    await _require_support()
    db = get_db_connection()
    account_repo = DavAccountRepository(db)
    account = await account_repo.get_by_id(request.dav_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="DAV account not found")

    async with db.session() as session:
        collection = await create_collection(
            session, dav_account_id=request.dav_account_id, kind="addressbook",
            slug=_slugify(request.display_name), display_name=request.display_name,
        )
    return AddressbookSummaryResponse(
        id=collection.id, dav_account_id=account.id, dav_account_name=account.name,
        display_name=collection.display_name or collection.slug,
        read_only=collection.read_only, total_count=collection.total_count,
    )


# --- The identity-to-calendar mapping ---


@links_router.get("", response_model=CalendarLinksOut)
async def get_calendar_links() -> CalendarLinksOut:
    """The whole mapping as one document: every identity, the calendars
    linked to it, and which one (if any) receives its invitations."""
    await _require_support()
    db = get_db_connection()
    async with db.session() as session:
        identity_result = await session.execute(select(Identity).order_by(Identity.created_at))
        identities = identity_result.scalars().all()
        prefs_result = await session.execute(select(CalendarPrefs))
        prefs_rows = prefs_result.scalars().all()

    by_identity: dict[uuid.UUID, list[CalendarPrefs]] = {}
    for prefs in prefs_rows:
        if prefs.identity_id is not None:
            by_identity.setdefault(prefs.identity_id, []).append(prefs)

    revision = await CalendarLinksRevisionRepository(db).current()
    rows = [
        CalendarLinkRowOut(
            identity_id=identity.id,
            identity_address=identity.email,
            account_id=identity.account_id,
            calendar_ids=[p.collection_id for p in by_identity.get(identity.id, [])],
            receives_invitations_calendar_id=next(
                (p.collection_id for p in by_identity.get(identity.id, []) if p.intake), None,
            ),
        )
        for identity in identities
    ]
    return CalendarLinksOut(base_revision=revision, rows=rows)


@links_router.put("", response_model=CalendarLinksOut)
async def update_calendar_links(request: CalendarLinksUpdateRequest) -> CalendarLinksOut:
    """Replace the whole mapping document. Every identity with linked
    calendars must name exactly one as receives_invitations_calendar_id;
    a calendar named by more than one identity is rejected outright."""
    await _require_support()
    db = get_db_connection()
    revision_repo = CalendarLinksRevisionRepository(db)
    current_revision = await revision_repo.current()
    if request.base_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail=f"base_revision {request.base_revision} is stale -- "
            f"current revision is {current_revision}",
        )

    seen_calendars: dict[uuid.UUID, uuid.UUID] = {}
    for row in request.rows:
        if row.calendar_ids and row.receives_invitations_calendar_id is None:
            raise HTTPException(
                status_code=422,
                detail=f"identity {row.identity_id} links calendars but names none "
                "as receives_invitations_calendar_id",
            )
        if (
            row.receives_invitations_calendar_id is not None
            and row.receives_invitations_calendar_id not in row.calendar_ids
        ):
            raise HTTPException(
                status_code=422,
                detail=f"identity {row.identity_id}'s receives_invitations_calendar_id "
                "is not among its own calendar_ids",
            )
        for calendar_id in row.calendar_ids:
            if calendar_id in seen_calendars and seen_calendars[calendar_id] != row.identity_id:
                raise HTTPException(
                    status_code=422,
                    detail=f"calendar {calendar_id} is linked to more than one identity",
                )
            seen_calendars[calendar_id] = row.identity_id

    async with db.session() as session:
        existing_result = await session.execute(
            select(CalendarPrefs).where(CalendarPrefs.identity_id.isnot(None))
        )
        existing = existing_result.scalars().all()
        new_links = {
            (row.identity_id, calendar_id)
            for row in request.rows
            for calendar_id in row.calendar_ids
        }
        for prefs in existing:
            assert prefs.identity_id is not None
            if (prefs.identity_id, prefs.collection_id) not in new_links:
                prefs.identity_id = None
                prefs.intake = False
        await session.flush()

        for row in request.rows:
            for calendar_id in row.calendar_ids:
                result = await session.execute(
                    select(CalendarPrefs).where(CalendarPrefs.collection_id == calendar_id)
                )
                link_prefs = result.scalar_one_or_none()
                if link_prefs is None:
                    link_prefs = CalendarPrefs(collection_id=calendar_id)
                    session.add(link_prefs)
                link_prefs.identity_id = row.identity_id
                link_prefs.intake = calendar_id == row.receives_invitations_calendar_id
        await session.flush()

        await revision_repo.bump(session)

    return await get_calendar_links()
