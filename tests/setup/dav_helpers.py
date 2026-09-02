"""
Raw CalDAV/CardDAV helpers against the test Radicale server, using nothing beyond
httpx and the standard library's XML support -- MailVerdict itself has no DAV client
and must never grow one; this is test/seed infrastructure only, the same role
tests/setup/mail_delivery.py plays by speaking raw LMTP.

Discovery follows the real protocol (current-user-principal, then the two home
sets off the principal) rather than hardcoding Radicale's URL layout, so a test
proves what a real client -- PostIMAP included -- would actually find.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx

_DAV_NS = "DAV:"
_CAL_NS = "urn:ietf:params:xml:ns:caldav"
_CARD_NS = "urn:ietf:params:xml:ns:carddav"

_PRINCIPAL_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop><D:current-user-principal/></D:prop>
</D:propfind>"""

_HOME_SETS_BODY = f"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="{_DAV_NS}" xmlns:C="{_CAL_NS}" xmlns:CARD="{_CARD_NS}">
  <D:prop>
    <C:calendar-home-set/>
    <CARD:addressbook-home-set/>
  </D:prop>
</D:propfind>""".encode()


def _propfind(client: httpx.Client, url: str, body: bytes) -> ET.Element:
    resp = client.request(
        "PROPFIND", url, content=body,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
    )
    if resp.status_code not in (200, 207):
        raise RuntimeError(f"PROPFIND {url} -> {resp.status_code}: {resp.text}")
    return ET.fromstring(resp.content)


def _href(root: ET.Element, tag: str, ns: str) -> str | None:
    el = root.find(f".//{{{ns}}}{tag}/{{{_DAV_NS}}}href")
    return el.text if el is not None and el.text else None


def discover_principal(client: httpx.Client, base_url: str) -> str:
    """current-user-principal, resolved to an absolute URL -- the same first
    request PostIMAP's own discovery makes, and what auto-creates the
    principal for a username Radicale has never seen."""
    root = _propfind(client, base_url, _PRINCIPAL_BODY)
    href = _href(root, "current-user-principal", _DAV_NS)
    if href is None:
        raise RuntimeError(f"No current-user-principal in PROPFIND response from {base_url}")
    return urljoin(base_url, href)


def discover_home_sets(client: httpx.Client, principal_url: str) -> tuple[str | None, str | None]:
    """calendar-home-set and addressbook-home-set off the principal URL."""
    root = _propfind(client, principal_url, _HOME_SETS_BODY)
    cal_href = _href(root, "calendar-home-set", _CAL_NS)
    ab_href = _href(root, "addressbook-home-set", _CARD_NS)
    calendar_home = urljoin(principal_url, cal_href) if cal_href else None
    addressbook_home = urljoin(principal_url, ab_href) if ab_href else None
    return calendar_home, addressbook_home


def mkcalendar(client: httpx.Client, url: str, display_name: str) -> None:
    """MKCALENDAR at `url`, which must end in '/'. RFC 4791 says MKCALENDAR only ever
    targets a non-existent resource; Radicale reports one already there as 405 or 409
    ('resource-must-be-null'), both treated as success here so seeding the same slug
    twice is idempotent."""
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<C:mkcalendar xmlns:D="{_DAV_NS}" xmlns:C="{_CAL_NS}">
  <D:set><D:prop><D:displayname>{display_name}</D:displayname></D:prop></D:set>
</C:mkcalendar>""".encode()
    resp = client.request(
        "MKCALENDAR", url, content=body,
        headers={"Content-Type": "application/xml; charset=utf-8"},
    )
    if resp.status_code not in (200, 201, 405, 409):
        raise RuntimeError(f"MKCALENDAR {url} -> {resp.status_code}: {resp.text}")


def mkcol_addressbook(client: httpx.Client, url: str, display_name: str) -> None:
    """Extended MKCOL at `url` (which must end in '/'), creating an address book. A
    405 or 409 means one already exists there -- treated as success, same as mkcalendar()."""
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<D:mkcol xmlns:D="{_DAV_NS}" xmlns:CARD="{_CARD_NS}">
  <D:set><D:prop>
    <D:resourcetype><D:collection/><CARD:addressbook/></D:resourcetype>
    <D:displayname>{display_name}</D:displayname>
  </D:prop></D:set>
</D:mkcol>""".encode()
    resp = client.request(
        "MKCOL", url, content=body,
        headers={"Content-Type": "application/xml; charset=utf-8"},
    )
    if resp.status_code not in (200, 201, 405, 409):
        raise RuntimeError(f"MKCOL {url} -> {resp.status_code}: {resp.text}")


def put_object(client: httpx.Client, url: str, data: str, content_type: str) -> None:
    """PUT one iCalendar or vCard resource at `url` (unconditional -- test setup
    only, no If-Match/If-None-Match negotiation needed)."""
    resp = client.put(url, content=data.encode("utf-8"), headers={"Content-Type": content_type})
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"PUT {url} -> {resp.status_code}: {resp.text}")


def get_object(client: httpx.Client, url: str) -> str:
    """GET one resource's body back, to assert what actually landed on the server."""
    resp = client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"GET {url} -> {resp.status_code}: {resp.text}")
    return resp.text


@dataclass
class TestPrincipal:
    """A discovered principal's two home collections, ready to create calendars
    or address books under."""

    base_url: str
    calendar_home: str | None
    addressbook_home: str | None


def discover(client: httpx.Client, base_url: str) -> TestPrincipal:
    """Discover a (possibly brand new) principal's home sets in one call."""
    principal = discover_principal(client, base_url)
    calendar_home, addressbook_home = discover_home_sets(client, principal)
    return TestPrincipal(base_url, calendar_home, addressbook_home)


def create_calendar(
    client: httpx.Client, principal: TestPrincipal, slug: str, display_name: str = "Test Calendar",
) -> str:
    """MKCALENDAR a fresh calendar under the discovered calendar home. Returns its URL."""
    if principal.calendar_home is None:
        raise RuntimeError("Principal has no calendar-home-set")
    url = urljoin(principal.calendar_home, f"{slug}/")
    mkcalendar(client, url, display_name)
    return url


def create_addressbook(
    client: httpx.Client, principal: TestPrincipal, slug: str, display_name: str = "Test Contacts",
) -> str:
    """MKCOL a fresh address book under the discovered addressbook home. Returns its URL."""
    if principal.addressbook_home is None:
        raise RuntimeError("Principal has no addressbook-home-set")
    url = urljoin(principal.addressbook_home, f"{slug}/")
    mkcol_addressbook(client, url, display_name)
    return url


def sample_event(
    uid: str,
    summary: str = "Sample Event",
    *,
    dtstart: str = "20260910T100000Z",
    dtend: str = "20260910T110000Z",
    description: str | None = None,
) -> str:
    """A minimal, valid VEVENT -- mirrors PostIMAP's own test fixture. `dtstart`/`dtend`
    are floating UTC timestamps in iCalendar's own basic format (YYYYMMDDTHHMMSSZ)."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//mail-verdict-test//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "DTSTAMP:20260901T120000Z",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{summary}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{description}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def sample_contact(uid: str, fn: str = "Sample Contact", email: str = "sample@example.com") -> str:
    """A minimal, valid VCARD -- mirrors PostIMAP's own test fixture."""
    return (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"UID:{uid}\r\n"
        f"FN:{fn}\r\n"
        f"EMAIL:{email}\r\n"
        "END:VCARD\r\n"
    )


def unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
