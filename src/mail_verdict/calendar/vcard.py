"""
Reading and building vCard (VCARD) bodies.

The contract's parsed columns on a dav_objects row of kind='addressbook'
are only `summary` (a vCard's FN) and `emails` -- everything else a
contact needs (organization, phones, addresses, birthday, URL, notes) has
to be read out of `data` here, server-side, rather than left to the UI to
parse vCard text itself.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import vobject


@dataclass
class ContactEmail:
    email: str
    type: str | None = None


@dataclass
class ContactPhone:
    number: str
    type: str | None = None


@dataclass
class ContactAddress:
    label: str | None
    text: str


@dataclass
class ContactPhoto:
    """`kind="embedded"` means `url` is a self-contained `data:` URI --
    already in the mirror, safe to render with no network request.
    `kind="url"` means `url` is a third party's address; a caller must
    run it through the same remote-content allowlist any other remote
    image does before ever putting it in an `<img src>`."""

    kind: Literal["embedded", "url"]
    url: str


@dataclass
class ParsedContact:
    summary: str
    emails: list[ContactEmail] = field(default_factory=list)
    organization: str | None = None
    title: str | None = None
    phones: list[ContactPhone] = field(default_factory=list)
    addresses: list[ContactAddress] = field(default_factory=list)
    birthday: str | None = None
    urls: list[str] = field(default_factory=list)
    notes: str | None = None
    categories: list[str] = field(default_factory=list)
    photo: ContactPhoto | None = None


def _type_param(line: object) -> str | None:
    types = getattr(line, "type_param", None)
    if not types:
        return None
    # vobject hands back a single string for one TYPE, a list for several
    # -- the UI only renders one badge per entry, so the first is enough.
    if isinstance(types, list):
        return str(types[0]) if types else None
    return str(types)


def _unfold_lines(data: str) -> list[str]:
    """RFC 6350 line unfolding: a physical line starting with a single
    SPACE or TAB continues the previous one."""
    raw_lines = data.replace("\r\n", "\n").split("\n")
    lines: list[str] = []
    for raw in raw_lines:
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_content_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """A best-effort split of one unfolded vCard content line into
    (name, params, value) -- not a general vCard parser, only precise
    enough to read PHOTO ourselves. See `_extract_photo` for why."""
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].rsplit(".", 1)[-1].upper()  # drop a vCard group prefix
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.upper()] = val
    return name, params, value


_MIME_BY_TYPE_PARAM = {
    "JPEG": "image/jpeg", "JPG": "image/jpeg", "PNG": "image/png",
    "GIF": "image/gif", "WEBP": "image/webp",
}


def _sniff_mime(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _extract_photo(data: str) -> ContactPhoto | None:
    """Read PHOTO directly off the raw vCard text rather than through
    vobject's own value transform. vobject's default TEXT-value decoding
    treats an unencoded value as a comma-separated list and keeps only
    the first field -- harmless for a v3.0 ENCODING=b photo (no comma
    appears before base64 decoding), but it silently truncates the
    common v4.0 `PHOTO:data:image/jpeg;base64,<payload>` shape at its
    own comma, discarding the entire payload."""
    for line in _unfold_lines(data):
        parsed = _split_content_line(line)
        if parsed is None:
            continue
        name, params, value = parsed
        if name != "PHOTO":
            continue
        value = value.strip()
        if not value:
            return None
        encoding = params.get("ENCODING", "").lower()
        if encoding in ("b", "base64") or "BASE64" in params:
            try:
                raw_bytes = base64.b64decode("".join(value.split()))
            except (ValueError, binascii.Error):
                return None
            mime = _MIME_BY_TYPE_PARAM.get(params.get("TYPE", "").upper()) or _sniff_mime(raw_bytes)
            encoded = base64.b64encode(raw_bytes).decode("ascii")
            return ContactPhoto(kind="embedded", url=f"data:{mime};base64,{encoded}")
        if value.startswith("data:"):
            return ContactPhoto(kind="embedded", url=value)
        return ContactPhoto(kind="url", url=value)
    return None


_DATA_URL_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?;base64,(.*)$", re.DOTALL)


def _decode_photo_data_url(data_url: str) -> tuple[str, bytes]:
    """The inverse of `_extract_photo`'s embedded case -- what the editor's
    file picker hands back after reading a chosen image as a data URL."""
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        raise ValueError("Photo must be a base64 data URL")
    mime = match.group(1) or "application/octet-stream"
    raw = base64.b64decode(match.group(2))
    return mime, raw


def _set_photo(card: Any, photo_data_url: str) -> None:
    """Always written as ENCODING=b -- vobject serializes that form
    without backslash-escaping it, so it round-trips through
    `_extract_photo` unchanged however the reading side got there."""
    mime, raw = _decode_photo_data_url(photo_data_url)
    line = card.add("photo")
    line.value = raw
    line.encoding_param = "b"
    subtype = mime.split("/", 1)[-1].upper()
    if subtype:
        line.type_param = subtype


def _address_text(value: object) -> str:
    """Render vobject's Address (street/city/region/code/country) as one
    display line -- the UI's ContactAddress carries no structured fields,
    only a label and free text."""
    parts = [
        getattr(value, "street", None),
        getattr(value, "city", None),
        getattr(value, "region", None),
        getattr(value, "code", None),
        getattr(value, "country", None),
    ]
    return ", ".join(p for p in parts if p)


def parse_contact(data: str) -> ParsedContact:
    """Parse a whole VCARD body into its structured fields."""
    card = vobject.readOne(data)

    summary = str(card.fn.value) if hasattr(card, "fn") else ""

    emails = [
        ContactEmail(email=str(line.value).strip(), type=_type_param(line))
        for line in getattr(card, "email_list", [])
    ]

    organization = None
    if hasattr(card, "org"):
        org_value = card.org.value
        organization = ", ".join(org_value) if isinstance(org_value, list) else str(org_value)

    title = str(card.title.value) if hasattr(card, "title") else None

    phones = [
        ContactPhone(number=str(line.value), type=_type_param(line))
        for line in getattr(card, "tel_list", [])
    ]

    addresses = [
        ContactAddress(label=_type_param(line), text=_address_text(line.value))
        for line in getattr(card, "adr_list", [])
    ]

    birthday = None
    if hasattr(card, "bday"):
        bday_value = card.bday.value
        birthday = str(bday_value.date() if hasattr(bday_value, "date") else bday_value)

    urls = [str(line.value) for line in getattr(card, "url_list", [])]
    notes = str(card.note.value) if hasattr(card, "note") else None

    categories: list[str] = []
    if hasattr(card, "categories"):
        cat_value = card.categories.value
        categories = list(cat_value) if isinstance(cat_value, list) else [str(cat_value)]

    photo = _extract_photo(data)

    return ParsedContact(
        summary=summary, emails=emails, organization=organization, title=title,
        phones=phones, addresses=addresses, birthday=birthday, urls=urls, notes=notes,
        categories=categories, photo=photo,
    )


def build_contact(
    *,
    summary: str,
    emails: list[ContactEmail],
    organization: str | None = None,
    title: str | None = None,
    phones: list[ContactPhone] | None = None,
    addresses: list[ContactAddress] | None = None,
    birthday: str | None = None,
    urls: list[str] | None = None,
    notes: str | None = None,
    categories: list[str] | None = None,
    photo_data_url: str | None = None,
) -> str:
    """Build a fresh VCARD body with a new UID."""
    card = vobject.vCard()
    card.add("uid").value = str(uuid.uuid4())
    card.add("fn").value = summary
    # N is required by RFC 6350; a single free-text name is not
    # structured, so it goes entirely in the family-name slot rather than
    # guessing a first/last split from `summary`.
    name = card.add("n")
    name.value = vobject.vcard.Name(family=summary)

    for contact_email in emails:
        line = card.add("email")
        line.value = contact_email.email
        if contact_email.type:
            line.type_param = contact_email.type

    if organization:
        card.add("org").value = [organization]
    if title:
        card.add("title").value = title

    for phone in phones or []:
        line = card.add("tel")
        line.value = phone.number
        if phone.type:
            line.type_param = phone.type

    for address in addresses or []:
        line = card.add("adr")
        line.value = vobject.vcard.Address(street=address.text)
        if address.label:
            line.type_param = address.label

    if birthday:
        card.add("bday").value = birthday
    for contact_url in urls or []:
        card.add("url").value = contact_url
    if notes:
        card.add("note").value = notes
    if categories:
        card.add("categories").value = categories
    if photo_data_url:
        _set_photo(card, photo_data_url)

    return str(card.serialize())


def apply_contact_fields(
    data: str,
    *,
    summary: str | None = None,
    emails: list[ContactEmail] | None = None,
    organization: str | None = None,
    title: str | None = None,
    phones: list[ContactPhone] | None = None,
    addresses: list[ContactAddress] | None = None,
    birthday: str | None = None,
    urls: list[str] | None = None,
    notes: str | None = None,
    categories: list[str] | None = None,
    photo_data_url: str | None = None,
) -> str:
    """
    Edit an existing VCARD in place. Every field is a full replacement of
    that property when given (the UI sends the complete list, e.g. every
    email, not a delta) -- fields left as None are untouched. UID and any
    property this application does not model are preserved verbatim.
    """
    card = vobject.readOne(data)

    if summary is not None:
        if hasattr(card, "fn"):
            card.fn.value = summary
        else:
            card.add("fn").value = summary

    if emails is not None:
        for line in list(getattr(card, "email_list", [])):
            card.remove(line)
        for contact_email in emails:
            line = card.add("email")
            line.value = contact_email.email
            if contact_email.type:
                line.type_param = contact_email.type

    if organization is not None:
        if hasattr(card, "org"):
            card.remove(card.org)
        if organization:
            card.add("org").value = [organization]

    if title is not None:
        if hasattr(card, "title"):
            card.remove(card.title)
        if title:
            card.add("title").value = title

    if phones is not None:
        for line in list(getattr(card, "tel_list", [])):
            card.remove(line)
        for phone in phones:
            line = card.add("tel")
            line.value = phone.number
            if phone.type:
                line.type_param = phone.type

    if addresses is not None:
        for line in list(getattr(card, "adr_list", [])):
            card.remove(line)
        for address in addresses:
            line = card.add("adr")
            line.value = vobject.vcard.Address(street=address.text)
            if address.label:
                line.type_param = address.label

    if birthday is not None:
        if hasattr(card, "bday"):
            card.remove(card.bday)
        if birthday:
            card.add("bday").value = birthday

    if urls is not None:
        for line in list(getattr(card, "url_list", [])):
            card.remove(line)
        for contact_url in urls:
            card.add("url").value = contact_url

    if notes is not None:
        if hasattr(card, "note"):
            card.remove(card.note)
        if notes:
            card.add("note").value = notes

    if categories is not None:
        # A card can legally carry more than one CATEGORIES line -- some
        # servers produce that shape. `card.categories` (like `card.photo`
        # below) is vobject's singular accessor and only ever names the
        # first, which would leave every other one behind.
        for line in list(getattr(card, "categories_list", [])):
            card.remove(line)
        if categories:
            card.add("categories").value = categories

    if photo_data_url is not None:
        for line in list(getattr(card, "photo_list", [])):
            card.remove(line)
        if photo_data_url:
            _set_photo(card, photo_data_url)

    return str(card.serialize())
