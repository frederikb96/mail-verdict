"""
Contact API endpoints -- dav_objects of kind='addressbook'.

GET    /api/contacts              -- list, paged, optional address-book filter and search
GET    /api/contacts/search       -- one row per email address, for compose autocomplete
GET    /api/contacts/:id          -- structured detail, parsed from the vCard body
POST   /api/contacts              -- create
PATCH  /api/contacts/:id          -- edit (full replacement per given field)
DELETE /api/contacts/:id          -- delete

Requires PostIMAP >= 1.6.0 -- see postimap/contract.py's MIN_DAV_SERVICE_VERSION.
"""

from __future__ import annotations

import base64
import uuid

from fastapi import APIRouter, HTTPException, Query

from mail_verdict.api.schemas import (
    ContactAddressIO,
    ContactCreateRequest,
    ContactEmailIO,
    ContactListResponse,
    ContactPhoneIO,
    ContactResponse,
    ContactSearchHitOut,
    ContactUpdateRequest,
)
from mail_verdict.calendar import vcard
from mail_verdict.calendar.repository import CollectionRepository, DavObjectRepository
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import DavObject
from mail_verdict.postimap.actions import create_object, delete_object, replace_object_data
from mail_verdict.postimap.contract import read_postimap_info, supports_dav

router = APIRouter(prefix="/contacts", tags=["contacts"])

_UNSUPPORTED_DETAIL = (
    "Calendars and contacts require PostIMAP service_version >= 1.6.0; "
    "the running instance reports {version}."
)

# Cursor encodes a plain integer offset -- contacts have no natural
# ordering key beyond `summary`, which can repeat, so an opaque
# offset-in-a-string is the simplest cursor that still hides the
# implementation from the client.
_DEFAULT_PAGE_SIZE = 50


async def _require_support() -> None:
    db = get_db_connection()
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_dav(info):
        raise HTTPException(
            status_code=501,
            detail=_UNSUPPORTED_DETAIL.format(version=info.service_version if info else "unknown"),
        )


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc


async def _to_response(obj: DavObject) -> ContactResponse:
    parsed = vcard.parse_contact(obj.data)
    collection_repo = CollectionRepository(get_db_connection())
    collection = await collection_repo.get_by_id(obj.collection_id)
    return ContactResponse(
        id=obj.id,
        addressbook_id=obj.collection_id,
        addressbook_name=(collection.display_name or collection.slug) if collection else "",
        read_only=collection.read_only if collection else False,
        summary=parsed.summary,
        emails=[ContactEmailIO(email=e.email, type=e.type) for e in parsed.emails],
        organization=parsed.organization,
        title=parsed.title,
        phones=[ContactPhoneIO(number=p.number, type=p.type) for p in parsed.phones],
        addresses=[ContactAddressIO(label=a.label, text=a.text) for a in parsed.addresses],
        birthday=parsed.birthday,
        url=parsed.url,
        notes=parsed.notes,
    )


@router.get("", response_model=ContactListResponse)
async def list_contacts(
    addressbook_id: uuid.UUID | None = None,
    q: str | None = None,
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=200),
    cursor: str | None = None,
) -> ContactListResponse:
    """List contacts, paged -- never an unpaged fetch, an address book can
    hold thousands of rows."""
    await _require_support()
    offset = _decode_cursor(cursor)
    repo = DavObjectRepository(get_db_connection())
    addressbook_ids = [addressbook_id] if addressbook_id is not None else None
    rows, has_more = await repo.search_contacts(addressbook_ids, q, limit=limit, offset=offset)
    contacts = [await _to_response(row) for row in rows]
    next_cursor = _encode_cursor(offset + limit) if has_more else None
    return ContactListResponse(contacts=contacts, has_more=has_more, next_cursor=next_cursor)


@router.get("/search", response_model=list[ContactSearchHitOut])
async def search_contacts(q: str = Query(min_length=1)) -> list[ContactSearchHitOut]:
    """One row per email address -- a contact with three addresses is
    three choices in the compose autocomplete."""
    await _require_support()
    repo = DavObjectRepository(get_db_connection())
    hits = await repo.search_email_hits(q, limit=20)
    results: list[ContactSearchHitOut] = []
    for obj in hits:
        parsed = vcard.parse_contact(obj.data)
        for email in parsed.emails:
            results.append(
                ContactSearchHitOut(
                    contact_id=obj.id, name=parsed.summary, email=email.email, source="contact",
                )
            )
    return results


@router.get("/{contact_id}", response_model=ContactResponse)
async def get_contact(contact_id: uuid.UUID) -> ContactResponse:
    """Structured detail, parsed server-side from the vCard body."""
    await _require_support()
    repo = DavObjectRepository(get_db_connection())
    obj = await repo.get_by_id(contact_id)
    if obj is None or obj.deleted_at is not None or obj.kind != "addressbook":
        raise HTTPException(status_code=404, detail="Contact not found")
    return await _to_response(obj)


@router.post("", response_model=ContactResponse, status_code=201)
async def create_contact(request: ContactCreateRequest) -> ContactResponse:
    """Create a contact in an address book."""
    await _require_support()
    db = get_db_connection()
    collection_repo = CollectionRepository(db)
    collection = await collection_repo.get_by_id(request.addressbook_id)
    if collection is None or collection.kind != "addressbook":
        raise HTTPException(status_code=404, detail="Address book not found")

    data = vcard.build_contact(
        summary=request.summary,
        emails=[vcard.ContactEmail(email=e.email, type=e.type) for e in request.emails],
        organization=request.organization,
        title=request.title,
        phones=[vcard.ContactPhone(number=p.number, type=p.type) for p in request.phones],
        addresses=[vcard.ContactAddress(label=a.label, text=a.text) for a in request.addresses],
        birthday=request.birthday,
        url=request.url,
        notes=request.notes,
    )
    async with db.session() as session:
        obj = await create_object(
            session, dav_account_id=collection.account_id,
            collection_id=request.addressbook_id, data=data,
        )
    return await _to_response(obj)


@router.patch("/{contact_id}", response_model=ContactResponse)
async def update_contact(contact_id: uuid.UUID, request: ContactUpdateRequest) -> ContactResponse:
    """Edit a contact -- every field given is a full replacement of that
    property (e.g. the whole email list), matching what the UI sends."""
    await _require_support()
    db = get_db_connection()
    repo = DavObjectRepository(db)
    obj = await repo.get_by_id(contact_id)
    if obj is None or obj.deleted_at is not None or obj.kind != "addressbook":
        raise HTTPException(status_code=404, detail="Contact not found")

    values = request.model_dump(exclude_unset=True)
    emails = None
    if "emails" in values:
        emails = [
            vcard.ContactEmail(email=e["email"], type=e.get("type")) for e in values["emails"]
        ]
    phones = None
    if "phones" in values:
        phones = [
            vcard.ContactPhone(number=p["number"], type=p.get("type")) for p in values["phones"]
        ]
    addresses = None
    if "addresses" in values:
        addresses = [
            vcard.ContactAddress(label=a.get("label"), text=a["text"]) for a in values["addresses"]
        ]
    updated_data = vcard.apply_contact_fields(
        obj.data,
        summary=values.get("summary"),
        emails=emails,
        organization=values.get("organization"),
        title=values.get("title"),
        phones=phones,
        addresses=addresses,
        birthday=values.get("birthday"),
        url=values.get("url"),
        notes=values.get("notes"),
    )
    async with db.session() as session:
        await replace_object_data(session, contact_id, updated_data)
    refreshed = await repo.get_by_id(contact_id)
    assert refreshed is not None
    return await _to_response(refreshed)


@router.delete("/{contact_id}", status_code=204)
async def delete_contact(contact_id: uuid.UUID) -> None:
    """Delete a contact."""
    await _require_support()
    db = get_db_connection()
    repo = DavObjectRepository(db)
    obj = await repo.get_by_id(contact_id)
    if obj is None or obj.deleted_at is not None or obj.kind != "addressbook":
        raise HTTPException(status_code=404, detail="Contact not found")
    async with db.session() as session:
        await delete_object(session, contact_id)
