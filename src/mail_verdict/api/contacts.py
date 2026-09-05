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

import asyncio
import base64
import concurrent.futures
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Response

from mail_verdict.api.image_exceptions import is_sender_image_allowed
from mail_verdict.api.schemas import (
    ContactAddressIO,
    ContactCreateRequest,
    ContactEmailIO,
    ContactListResponse,
    ContactPhoneIO,
    ContactPhotoIndexEntry,
    ContactPhotoIndexResponse,
    ContactPhotoOut,
    ContactResponse,
    ContactSearchHitOut,
    ContactUpdateRequest,
)
from mail_verdict.calendar import vcard
from mail_verdict.calendar.repository import CollectionRepository, DavObjectRepository
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import DavCollection, DavObject
from mail_verdict.postimap.actions import create_object, delete_object, replace_object_data
from mail_verdict.postimap.contract import read_postimap_info, supports_dav

logger = logging.getLogger(__name__)

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


def _photo_out(contact_id: uuid.UUID, photo: vcard.ContactPhoto | None) -> ContactPhotoOut | None:
    """An embedded photo is always this application's own streaming
    endpoint, never an inline `data:` URI -- a browser only fetches it
    for a contact actually rendered on screen, and caches it after
    that. A third-party `kind="url"` photo is passed through unchanged;
    `parse_contact()` already only reports one once its own allowlist
    check (photo-index) or nothing at all (everywhere else) permits it."""
    if photo is None:
        return None
    url = f"/api/contacts/{contact_id}/photo" if photo.kind == "embedded" else photo.url
    return ContactPhotoOut(kind=photo.kind, url=url)


async def _to_response(
    obj: DavObject, *, collection: DavCollection | None = None,
) -> ContactResponse:
    """Structured detail for one contact. Never decodes an embedded
    photo's bytes -- see `_photo_out()` -- so a caller already holding
    the contact's address book (a page of `list_contacts`) can pass it
    in and skip re-fetching the same handful of collections per row."""
    parsed = vcard.parse_contact(obj.data, decode_photo=False)
    if collection is None:
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
        urls=parsed.urls,
        notes=parsed.notes,
        categories=parsed.categories,
        photo=_photo_out(obj.id, parsed.photo),
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
    # A Nextcloud address-book group is stored as an ordinary vCard --
    # PostIMAP has no concept of one -- so it must never reach the list
    # looking like a person with no address.
    rows = [row for row in rows if not vcard.is_group(row.data)]
    # One query for every address book a row on this page belongs to,
    # instead of one per row -- almost every contact on a page shares
    # the same handful of address books.
    collection_repo = CollectionRepository(get_db_connection())
    collections = await collection_repo.get_by_ids(list({row.collection_id for row in rows}))
    contacts = [
        await _to_response(row, collection=collections.get(row.collection_id)) for row in rows
    ]
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
        parsed = vcard.parse_contact(obj.data, decode_photo=False)
        for email in parsed.emails:
            results.append(
                ContactSearchHitOut(
                    contact_id=obj.id, name=parsed.summary, email=email.email, source="contact",
                )
            )
    return results


@router.get("/resolve", response_model=ContactResponse | None)
async def resolve_contact_by_email(email: str = Query(min_length=1)) -> ContactResponse | None:
    """The one contact carrying this address, or none -- what a sender's
    avatar/name lookup resolves against. `None` (204-less null body) is
    the ordinary "no match" outcome, not an error."""
    await _require_support()
    repo = DavObjectRepository(get_db_connection())
    obj = await repo.find_by_email(email)
    if obj is None:
        return None
    return await _to_response(obj)


# A thread that outlives its own timeout keeps occupying whatever pool it
# was submitted to until it eventually finishes on its own -- see
# api/calendar_events.py's identical `_EXPANSION_EXECUTOR`, the pattern
# this copies. A dedicated, bounded pool contains that to the photo scan
# alone, rather than letting one pathological or oversized address book
# eventually starve every unrelated asyncio.to_thread() call sharing the
# loop's own default executor.
_PHOTO_SCAN_TIMEOUT_SECONDS = 10.0
_PHOTO_SCAN_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="contacts-photo-scan",
)

# A url-kind photo's own candidate emails, deferred out of the thread pool
# since resolving them needs an awaited, per-account allowlist check that
# has to run back on the event loop.
_UrlPhotoCandidate = tuple[uuid.UUID, str, list[str]]


def _scan_photos_sync(
    rows: list[tuple[uuid.UUID, str]],
) -> tuple[dict[str, ContactPhotoIndexEntry], list[_UrlPhotoCandidate]]:
    embedded: dict[str, ContactPhotoIndexEntry] = {}
    url_candidates: list[_UrlPhotoCandidate] = []
    for contact_id, data in rows:
        try:
            parsed = vcard.parse_contact(data, decode_photo=False)
        except Exception:
            # A single malformed vCard must never take the whole index
            # down with it -- catch broadly, the same reasoning
            # calendar_events.py's own _expand_all_sync applies to a
            # parse failure there.
            logger.warning("Skipping contact %s in photo index", contact_id, exc_info=True)
            continue
        if parsed.photo is None or not parsed.emails or vcard.is_group(data):
            continue
        if parsed.photo.kind == "embedded":
            entry = ContactPhotoIndexEntry(
                contact_id=contact_id, photo_url=f"/api/contacts/{contact_id}/photo",
            )
            for contact_email in parsed.emails:
                embedded[contact_email.email.strip().lower()] = entry
        else:
            url_candidates.append(
                (contact_id, parsed.photo.url, [e.email for e in parsed.emails])
            )
    return embedded, url_candidates


async def _scan_photos(
    rows: list[tuple[uuid.UUID, str]],
) -> tuple[dict[str, ContactPhotoIndexEntry], list[_UrlPhotoCandidate]]:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_PHOTO_SCAN_EXECUTOR, _scan_photos_sync, rows),
            timeout=_PHOTO_SCAN_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Photo index scan exceeded %.0fs for %d contacts; returning none of them",
            _PHOTO_SCAN_TIMEOUT_SECONDS, len(rows),
        )
        return {}, []


@router.get("/photo-index", response_model=ContactPhotoIndexResponse)
async def get_photo_index(
    account_id: uuid.UUID | None = Query(default=None),
) -> ContactPhotoIndexResponse:
    """
    The whole address book's sender-avatar photos, by lower-cased email
    -- one request for a mail or search list to cache (a long staleTime,
    read synchronously as rows render) and never repeat per row or per
    sender scrolled into view; a virtualized list over many thousand
    messages cannot afford a network call tied to a row entering the
    viewport.

    No photo bytes travel here regardless of how large or how numerous
    the address book's own photos are: an embedded photo's `photo_url`
    is this application's own `GET /contacts/:id/photo`, which a caller
    only ever fetches for a contact actually rendered on screen, and the
    browser caches after that. A `kind="url"` photo is included only
    once `account_id` is given and `is_sender_image_allowed` says that
    address is on its allowlist -- the identical rule and the identical
    check a message's own remote images are gated by; omitted otherwise,
    the same as a contact with no photo at all.

    The address book is read whole -- there is no cheaper affordance
    upstream to page it with -- but scanning it for photos runs off the
    event loop with a bounded timeout (`_scan_photos`), so a large one
    slows this request rather than every request the server is
    currently handling.
    """
    await _require_support()
    repo = DavObjectRepository(get_db_connection())
    rows = await repo.list_ids_and_data()
    by_email, url_candidates = await _scan_photos(rows)
    if account_id is not None:
        for contact_id, url, emails in url_candidates:
            entry = ContactPhotoIndexEntry(contact_id=contact_id, photo_url=url)
            for email in emails:
                if await is_sender_image_allowed(account_id, email):
                    by_email[email.strip().lower()] = entry
    return ContactPhotoIndexResponse(by_email=by_email)


@router.get("/{contact_id}/photo")
async def get_contact_photo(contact_id: uuid.UUID) -> Response:
    """Stream an embedded contact photo's decoded bytes -- what the
    photo index's `photo_url` points to for a `kind="embedded"` entry,
    and what every other contact response's own `photo.url` now points
    to as well. The one place a photo is actually decoded, for one
    contact at a time, on request -- a `kind="url"` photo has no bytes
    to stream here (a stored value that will not decode looks the same
    to a caller: a card with no usable photo, not a fault in this
    request, since a server can truncate a long PHOTO value on write
    and the card then keeps an unusable one indefinitely)."""
    await _require_support()
    repo = DavObjectRepository(get_db_connection())
    obj = await repo.get_by_id(contact_id)
    if obj is None or obj.deleted_at is not None or obj.kind != "addressbook":
        raise HTTPException(status_code=404, detail="Contact not found")
    decoded = vcard.extract_photo_bytes(obj.data)
    if decoded is None:
        raise HTTPException(status_code=404, detail="Contact has no embedded photo")
    mime, raw = decoded
    return Response(
        content=raw,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=86400"},
    )


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
        urls=request.urls,
        notes=request.notes,
        categories=request.categories,
        photo_data_url=request.photo_data_url,
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
        urls=values.get("urls"),
        notes=values.get("notes"),
        categories=values.get("categories"),
        photo_data_url=values.get("photo_data_url"),
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
