"""
Reading and building vCard (VCARD) bodies.

The contract's parsed columns on a dav_objects row of kind='addressbook'
are only `summary` (a vCard's FN) and `emails` -- everything else a
contact needs (organization, phones, addresses, birthday, URL, notes) has
to be read out of `data` here, server-side, rather than left to the UI to
parse vCard text itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

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
class ParsedContact:
    summary: str
    emails: list[ContactEmail] = field(default_factory=list)
    organization: str | None = None
    title: str | None = None
    phones: list[ContactPhone] = field(default_factory=list)
    addresses: list[ContactAddress] = field(default_factory=list)
    birthday: str | None = None
    url: str | None = None
    notes: str | None = None


def _type_param(line: object) -> str | None:
    types = getattr(line, "type_param", None)
    if not types:
        return None
    # vobject hands back a single string for one TYPE, a list for several
    # -- the UI only renders one badge per entry, so the first is enough.
    if isinstance(types, list):
        return str(types[0]) if types else None
    return str(types)


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

    url = str(card.url.value) if hasattr(card, "url") else None
    notes = str(card.note.value) if hasattr(card, "note") else None

    return ParsedContact(
        summary=summary, emails=emails, organization=organization, title=title,
        phones=phones, addresses=addresses, birthday=birthday, url=url, notes=notes,
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
    url: str | None = None,
    notes: str | None = None,
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
    if url:
        card.add("url").value = url
    if notes:
        card.add("note").value = notes

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
    url: str | None = None,
    notes: str | None = None,
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

    if url is not None:
        if hasattr(card, "url"):
            card.remove(card.url)
        if url:
            card.add("url").value = url

    if notes is not None:
        if hasattr(card, "note"):
            card.remove(card.note)
        if notes:
            card.add("note").value = notes

    return str(card.serialize())
