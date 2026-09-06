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
import time
import uuid

from fastapi import APIRouter, HTTPException, Query, Response

from mail_verdict.api.image_exceptions import is_sender_image_allowed
from mail_verdict.api.schemas import (
    ContactAddressIO,
    ContactCreateRequest,
    ContactEmailIO,
    ContactGroupOut,
    ContactGroupsResponse,
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


def _build_response(
    contact_id: uuid.UUID,
    collection_id: uuid.UUID,
    parsed: vcard.ParsedContact,
    collection: DavCollection | None,
) -> ContactResponse:
    """One contact's response from an already-parsed card -- so a caller
    that parsed a whole page off the event loop builds its rows without
    parsing anything again."""
    return ContactResponse(
        id=contact_id,
        addressbook_id=collection_id,
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
        photo=_photo_out(contact_id, parsed.photo),
    )


async def _to_response(
    obj: DavObject, *, collection: DavCollection | None = None,
) -> ContactResponse:
    """Structured detail for one contact. Never decodes an embedded
    photo's bytes -- see `_photo_out()` -- so a caller already holding
    the contact's address book can pass it in and skip re-fetching the
    same collection per row."""
    parsed = vcard.parse_contact(obj.data, decode_photo=False)
    if collection is None:
        collection_repo = CollectionRepository(get_db_connection())
        collection = await collection_repo.get_by_id(obj.collection_id)
    return _build_response(obj.id, obj.collection_id, parsed, collection)


@router.get("", response_model=ContactListResponse)
async def list_contacts(
    addressbook_id: uuid.UUID | None = None,
    q: str | None = None,
    group: str | None = None,
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=200),
    cursor: str | None = None,
) -> ContactListResponse:
    """List contacts, paged -- never an unpaged fetch, an address book can
    hold thousands of rows.

    `group` is one of the `id`s `GET /contacts/groups` hands back.
    `group_card:<id>` resolves to that card's own member uids and narrows
    in SQL, the same way `addressbook_id` already does. `category:<name>`
    cannot: CATEGORIES is not a column PostIMAP parses, so it is checked
    per card during the same drop-and-refill pass that already removes
    group cards from the page below -- a card failing either check is
    dropped, and the loop reads on rather than handing back a short page.

    Group cards are dropped after the database has already applied the
    limit, so a page can come back short of what was asked for; it is
    refilled by reading on from where the last batch ended rather than
    by returning fewer rows than requested. The cursor is therefore how
    far into the underlying order this page read, not a multiple of the
    page size -- a client counting rows and a server counting rows agree
    however many groups or non-matching cards the book holds."""
    await _require_support()
    scanned = _decode_cursor(cursor)
    repo = DavObjectRepository(get_db_connection())
    addressbook_ids = [addressbook_id] if addressbook_id is not None else None

    uid_in: list[str] | None = None
    category: str | None = None
    if group is not None and group.startswith("group_card:"):
        try:
            card_id = uuid.UUID(group.removeprefix("group_card:"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid group") from exc
        card = await repo.get_by_id(card_id)
        if card is None or card.deleted_at is not None:
            return ContactListResponse(contacts=[], has_more=False, next_cursor=None)
        uid_in = vcard.detect_group_members(card.data)
        if not uid_in:
            return ContactListResponse(contacts=[], has_more=False, next_cursor=None)
        # A group card's members are its own address book's contacts; keep
        # that scope even when no addressbook_id was given, so a uid that
        # happens to collide in another account's book is never pulled in.
        if addressbook_ids is None:
            addressbook_ids = [card.collection_id]
    elif group is not None and group.startswith("category:"):
        category = group.removeprefix("category:")

    parsed_rows: list[tuple[uuid.UUID, uuid.UUID, vcard.ParsedContact]] = []
    has_more = False
    while len(parsed_rows) < limit:
        rows, has_more = await repo.search_contacts(
            addressbook_ids, q, limit=limit - len(parsed_rows), offset=scanned, uid_in=uid_in,
        )
        if not rows:
            break
        scanned += len(rows)
        page = await _parse_page([(row.id, row.collection_id, row.data) for row in rows])
        if category is not None:
            page = [entry for entry in page if category in entry[2].categories]
        parsed_rows.extend(page)
        if not has_more:
            break
    # One query for every address book a row on this page belongs to,
    # instead of one per row -- almost every contact on a page shares
    # the same handful of address books.
    collection_repo = CollectionRepository(get_db_connection())
    collections = await collection_repo.get_by_ids(
        list({collection_id for _, collection_id, _ in parsed_rows})
    )
    contacts = [
        _build_response(contact_id, collection_id, parsed, collections.get(collection_id))
        for contact_id, collection_id, parsed in parsed_rows
    ]
    next_cursor = _encode_cursor(scanned) if has_more else None
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
# this copies. A dedicated, bounded pool contains that to reading cards
# alone, rather than letting one pathological or oversized address book
# eventually starve every unrelated asyncio.to_thread() call sharing the
# loop's own default executor.
_CARD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="contacts-cards",
)

# The photo scan stops itself at this point and reports what it has. The
# outer wait below is a backstop for a thread that never reaches its own
# deadline check at all, and only that path can produce nothing.
_PHOTO_SCAN_BUDGET_SECONDS = 8.0
_PHOTO_SCAN_TIMEOUT_SECONDS = 10.0
_DEADLINE_CHECK_EVERY = 32

# A url-kind photo's own candidate emails, deferred out of the thread pool
# since resolving them needs an awaited, per-account allowlist check that
# has to run back on the event loop.
_UrlPhotoCandidate = tuple[uuid.UUID, str, list[str]]


def _scan_photos_sync(
    rows: list[tuple[uuid.UUID, str, list[str] | None]], deadline: float,
) -> tuple[dict[str, ContactPhotoIndexEntry], list[_UrlPhotoCandidate], bool]:
    """Whether each card carries a photo and which addresses it is
    reachable at -- never a full parse. A card is mostly its embedded
    photo, and a general parser's cost is proportional to what it is
    handed, so asking `detect_photo`/`is_group` the two questions this
    scan actually has is an order of magnitude cheaper over an address
    book of any size. The addresses come from the column PostIMAP
    already parses EMAIL into, so they usually cost nothing at all.

    Returns what it read plus whether it stopped early."""
    embedded: dict[str, ContactPhotoIndexEntry] = {}
    url_candidates: list[_UrlPhotoCandidate] = []
    for index, (contact_id, data, column_emails) in enumerate(rows):
        if index % _DEADLINE_CHECK_EVERY == 0 and time.monotonic() >= deadline:
            return embedded, url_candidates, True
        try:
            photo = vcard.detect_photo(data)
            if photo is None or vcard.is_group(data):
                continue
            # The column is empty for a card this application has just
            # created and PostIMAP has not parsed back yet; reading the
            # addresses off that card costs the same walk again, and
            # only for those.
            emails = column_emails or vcard.detect_emails(data)
            if not emails:
                continue
        except Exception:
            # A single malformed vCard must never take the whole index
            # down with it -- catch broadly, the same reasoning
            # calendar_events.py's own _expand_all_sync applies to a
            # parse failure there.
            logger.warning("Skipping contact %s in photo index", contact_id, exc_info=True)
            continue
        if photo.kind == "embedded":
            entry = ContactPhotoIndexEntry(
                contact_id=contact_id, photo_url=f"/api/contacts/{contact_id}/photo",
            )
            for email in emails:
                embedded[email.strip().lower()] = entry
        else:
            url_candidates.append((contact_id, photo.url, list(emails)))
    return embedded, url_candidates, False


async def _scan_photos(
    rows: list[tuple[uuid.UUID, str, list[str] | None]],
) -> tuple[dict[str, ContactPhotoIndexEntry], list[_UrlPhotoCandidate], bool]:
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + _PHOTO_SCAN_BUDGET_SECONDS
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_CARD_EXECUTOR, _scan_photos_sync, rows, deadline),
            timeout=_PHOTO_SCAN_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Photo index scan exceeded %.0fs for %d contacts; returning none of them",
            _PHOTO_SCAN_TIMEOUT_SECONDS, len(rows),
        )
        return {}, [], True


def _scan_groups_sync(
    rows: list[tuple[uuid.UUID, uuid.UUID, str, str]], deadline: float,
) -> tuple[dict[str, int], list[tuple[uuid.UUID, str, int]], bool]:
    """Every CATEGORIES value and every group card found across `rows`,
    with a category's contact count and a group card's own member count
    -- the same "no general parse, proportional to the card's own text"
    shape `_scan_photos_sync` above uses, for the same reason: neither
    question is a column PostIMAP parses, and this reads a whole address
    book at once.

    Returns (category name -> contact count, [(card id, display name,
    member count)] for each group card), plus whether the scan stopped
    before finishing."""
    categories: dict[str, int] = {}
    group_cards: list[tuple[uuid.UUID, str, int]] = []
    for index, (card_id, _collection_id, summary, data) in enumerate(rows):
        if index % _DEADLINE_CHECK_EVERY == 0 and time.monotonic() >= deadline:
            return categories, group_cards, True
        try:
            if vcard.is_group(data):
                members = vcard.detect_group_members(data)
                group_cards.append((card_id, summary or "Group", len(members)))
                continue
            for category in vcard.detect_categories(data):
                categories[category] = categories.get(category, 0) + 1
        except Exception:
            # Same reasoning as the photo scan: one malformed card must
            # never take the whole index down with it.
            logger.warning("Skipping contact %s in groups index", card_id, exc_info=True)
            continue
    return categories, group_cards, False


async def _scan_groups(
    rows: list[tuple[uuid.UUID, uuid.UUID, str, str]],
) -> tuple[dict[str, int], list[tuple[uuid.UUID, str, int]], bool]:
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + _PHOTO_SCAN_BUDGET_SECONDS
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_CARD_EXECUTOR, _scan_groups_sync, rows, deadline),
            timeout=_PHOTO_SCAN_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Groups scan exceeded %.0fs for %d contacts; returning none of them",
            _PHOTO_SCAN_TIMEOUT_SECONDS, len(rows),
        )
        return {}, [], True


@router.get("/groups", response_model=ContactGroupsResponse)
async def get_contact_groups(
    addressbook_id: uuid.UUID | None = Query(default=None),
) -> ContactGroupsResponse:
    """Every group an address book's own contacts are actually in, for
    the groups filter beside the address-book filter -- an address book
    groups people two ways and a real one uses both, so both are offered
    rather than one being treated as the only kind that exists. `id` on
    each entry is what `list_contacts`'s own `group` param takes back.

    Scoped to `addressbook_id` when given; the whole mirror otherwise,
    the same default `GET /contacts/photo-index` already uses. `partial`
    carries the same meaning as that endpoint's: the scan ran out of
    budget, so an address book with more groups than shown may still
    have them."""
    await _require_support()
    repo = DavObjectRepository(get_db_connection())
    addressbook_ids = [addressbook_id] if addressbook_id is not None else None
    rows = await repo.list_group_scan_rows(addressbook_ids)
    categories, group_cards, partial = await _scan_groups(rows)
    groups = [
        ContactGroupOut(id=f"category:{name}", name=name, kind="category", count=count)
        for name, count in sorted(categories.items())
    ] + [
        ContactGroupOut(id=f"group_card:{card_id}", name=name, kind="group_card", count=count)
        for card_id, name, count in sorted(group_cards, key=lambda g: g[1])
    ]
    return ContactGroupsResponse(groups=groups, partial=partial)


def _parse_page_sync(
    rows: list[tuple[uuid.UUID, uuid.UUID, str]],
) -> list[tuple[uuid.UUID, uuid.UUID, vcard.ParsedContact]]:
    """Drop the page's group cards and parse the rest. Both questions
    are proportional to a card's own text, and a page of an address book
    carrying embedded photos is megabytes of it -- enough to stall every
    other request in the process for seconds if it ran on the loop."""
    parsed: list[tuple[uuid.UUID, uuid.UUID, vcard.ParsedContact]] = []
    for contact_id, collection_id, data in rows:
        # A Nextcloud address-book group is stored as an ordinary vCard --
        # PostIMAP has no concept of one -- so it must never reach the list
        # looking like a person with no address.
        if vcard.is_group(data):
            continue
        parsed.append((contact_id, collection_id, vcard.parse_contact(data, decode_photo=False)))
    return parsed


async def _parse_page(
    rows: list[tuple[uuid.UUID, uuid.UUID, str]],
) -> list[tuple[uuid.UUID, uuid.UUID, vcard.ParsedContact]]:
    """No budget, unlike the photo scan: a page is bounded by its own
    limit, and cutting one short would hand back fewer rows than were
    asked for -- exactly what the paging loop above exists to avoid."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_CARD_EXECUTOR, _parse_page_sync, rows)


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
    event loop with a bounded budget (`_scan_photos`), so a large one
    slows this request rather than every request the server is
    currently handling. A scan that runs out of budget returns the part
    of the book it did read and says so in `partial`, because a caller
    cannot otherwise tell an address book with no photos from one whose
    photos were never looked at.
    """
    await _require_support()
    repo = DavObjectRepository(get_db_connection())
    rows = await repo.list_photo_scan_rows()
    by_email, url_candidates, partial = await _scan_photos(rows)
    if account_id is not None:
        for contact_id, url, emails in url_candidates:
            entry = ContactPhotoIndexEntry(contact_id=contact_id, photo_url=url)
            for email in emails:
                if await is_sender_image_allowed(account_id, email):
                    by_email[email.strip().lower()] = entry
    return ContactPhotoIndexResponse(by_email=by_email, partial=partial)


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
