#!/usr/bin/env python3
"""Seed a development stack with a corpus the size of a real account.

`seed_dev.py` delivers a seventeen-message fixture corpus, a calendar with four
events and an address book with three people. That is enough to see a screen
render and not enough to see it behave: a list that never scrolls, a month view
with nothing to expand and an address book that fits on one page all look
perfect while being unusable at real sizes.

This seeds the shape a personal account actually has:

- a few thousand mail messages spread over several folders, threaded, with a
  long tail of dates
- thirty calendar collections of which most are to-do-only -- what a Nextcloud
  task list is -- holding thousands of objects between them, plus recurring
  series that started years ago
- an address book of a few thousand contacts across several books, a fifth of
  them carrying an embedded photo, grouped through `CATEGORIES` the way a
  Nextcloud address book groups them, plus `KIND:group` cards

Everything is written to the throwaway servers directly -- IMAP APPEND for
mail, CalDAV/CardDAV for the rest -- so it arrives through the same path real
data would, and PostIMAP mirrors it without knowing the difference. Run it
before the accounts are created and the first sync backfills the lot.

    python scripts/seed_large.py --imap-port 31143 --radicale-port 5232

`scripts/devstack.py --large` calls the same functions with its own random
ports, which is the usual way to reach it.

Sizes are proportional to `--scale`: 1.0 is the full corpus described above,
0.1 an ordinary tenth of it for a quick check that the seeding itself works.
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import imaplib
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.setup.dav_helpers import (  # noqa: E402
    create_addressbook,
    create_calendar,
    discover,
    put_object,
)

DEFAULT_DAV_USER = "alice"
DEFAULT_MAILBOX = "alice@test.local"
DEFAULT_IMAP_PASSWORD = "e2e-test-password"  # noqa: S105 -- throwaway container credential

# Mail lands here rather than only in INBOX, so the folder tree, per-folder
# counts and cross-folder search have something to work over.
MAIL_FOLDERS: list[tuple[str, float]] = [
    ("INBOX", 0.42),
    ("Archive", 0.30),
    ("Work", 0.12),
    ("Newsletters", 0.10),
    ("Family", 0.06),
]
MESSAGE_COUNT = 1200

# The proportions are Freddy's own account, measured: 25 of 30 collections are
# to-do-only and hold roughly 70% of all objects, which is exactly the shape
# that made the month view fetch thousands of objects that could never produce
# an event.
EVENT_CALENDARS = ["Work", "Family", "Sport", "Birthdays", "Travel"]
TASK_LISTS = [
    "Inbox", "Today", "Groceries", "Household", "Errands", "Reading", "Watch later",
    "Work backlog", "Work waiting", "Ideas", "Someday", "Bills", "Health", "Garden",
    "Bike", "Music", "Gifts", "Trip planning", "Repairs", "Learning", "Recipes",
    "Admin", "Renovation", "Volunteering", "Follow ups",
]
EVENTS_PER_CALENDAR = 250
TODOS_PER_LIST = 128

ADDRESS_BOOKS = ["Personal", "Work", "Family", "Clubs"]
CONTACT_COUNT = 1500
PHOTO_FRACTION = 0.2
PHOTO_BYTES = 30 * 1024
CONTACT_GROUPS = ["Family", "Work", "Uni", "Sport", "Neighbours", "Bookclub"]

FIRST_NAMES = [
    "Anna", "Ben", "Clara", "David", "Elif", "Finn", "Greta", "Hannes", "Ida", "Jonas",
    "Katrin", "Lukas", "Maja", "Noah", "Olivia", "Paul", "Quinn", "Rosa", "Sven", "Tara",
    "Ulrich", "Vera", "Wolf", "Xenia", "Yusuf", "Zoe", "Änne", "Örjan", "Übel", "Sören",
]
LAST_NAMES = [
    "Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker",
    "Schulz", "Hoffmann", "Koch", "Bauer", "Richter", "Klein", "Wolf", "Neumann",
    "Zimmermann", "Braun", "Krüger", "Hofmann", "Åberg", "Ó Braonáin", "Öztürk", "Ivanović",
]
ORGANISATIONS = [
    "Nordwind GmbH", "Helios AG", "Blaupunkt Labs", "Stadtwerke", "Uniklinik",
    "Freie Presse", "Kleinbahn e.V.", "Hafenkontor", "Waldhaus Schule", "",
]

SUBJECT_STEMS = [
    "Rechnung", "Terminbestätigung", "Weekly digest", "Pull request review",
    "Lieferung unterwegs", "Einladung zum Sommerfest", "Kontoauszug", "Deployment failed",
    "Neue Nachricht im Forum", "Mitgliedsbeitrag", "Reisekosten", "Statusbericht",
    "Newsletter", "Passwort zurücksetzen", "Fotos vom Wochenende", "Protokoll der Sitzung",
    "Angebot", "Erinnerung", "Reparaturtermin", "Vertragsverlängerung",
]
BODY_PARAGRAPHS = [
    "Kurz zur Info, damit du es auf dem Schirm hast.",
    "Die Unterlagen hängen an, Rückmeldung bis Freitag wäre gut.",
    "Der Termin steht, wir treffen uns wie besprochen.",
    "Falls das so nicht passt, sag einfach Bescheid.",
    "Thanks for the quick turnaround on this one.",
    "The build is green again after the revert, nothing else changed.",
    "Attached is the summary from the meeting, corrections welcome.",
    "No action needed, this is only for your records.",
]


def _rng(seed: int) -> random.Random:
    """A seeded generator, so two runs of this script produce the same corpus."""
    return random.Random(seed)


# --------------------------------------------------------------------------
# Mail
# --------------------------------------------------------------------------


def _message(rng: random.Random, index: int, thread: tuple[str, str] | None) -> bytes:
    """One RFC 5322 message. `thread` is an (message_id, subject) pair to reply
    to, which is what gives the corpus real conversations rather than 1200
    unrelated singletons."""
    sender_name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
    sender = f"sender{index % 137}@example.com"
    if thread is None:
        subject = f"{rng.choice(SUBJECT_STEMS)} {index}"
        references = ""
    else:
        parent_id, parent_subject = thread
        subject = parent_subject if parent_subject.startswith("Re: ") else f"Re: {parent_subject}"
        references = f"In-Reply-To: {parent_id}\r\nReferences: {parent_id}\r\n"
    age_days = rng.random() ** 2 * 900
    date = email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(days=age_days))
    message_id = f"<seed-{index}@example.com>"
    body = "\n\n".join(rng.sample(BODY_PARAGRAPHS, k=rng.randint(1, 3)))
    if index % 5 == 0:
        content_type = "text/html; charset=utf-8"
        rendered = "".join(f"<p>{line}</p>" for line in body.split("\n\n"))
        body = f"<html><body><h2>{subject}</h2>{rendered}</body></html>"
    else:
        content_type = "text/plain; charset=utf-8"
    raw = (
        f"From: {sender_name} <{sender}>\r\n"
        f"To: {DEFAULT_MAILBOX}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"{references}"
        "MIME-Version: 1.0\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
        f"{body}\r\n"
    )
    return raw.encode("utf-8")


def seed_mail(
    host: str, port: int, *, mailbox: str = DEFAULT_MAILBOX,
    password: str = DEFAULT_IMAP_PASSWORD, count: int = MESSAGE_COUNT, seed: int = 7,
) -> dict[str, int]:
    """APPEND a threaded corpus across several folders, creating the folders.

    APPEND rather than LMTP because LMTP can only ever deliver to INBOX, and a
    single folder is the one shape a folder tree cannot be tested in."""
    rng = _rng(seed)
    imap = imaplib.IMAP4(host, port)
    imap.login(mailbox, password)
    per_folder = {name: max(1, int(count * share)) for name, share in MAIL_FOLDERS}
    written: dict[str, int] = {}
    index = 0
    # Message-ID and subject of something already written to this folder, so a
    # reply threads onto a message the folder actually holds.
    for folder, wanted in per_folder.items():
        if folder != "INBOX":
            imap.create(folder)
        recent: list[tuple[str, str]] = []
        for _ in range(wanted):
            thread = rng.choice(recent) if recent and rng.random() < 0.35 else None
            raw = _message(rng, index, thread)
            subject = next(
                line.split(": ", 1)[1]
                for line in raw.decode().split("\r\n") if line.startswith("Subject: ")
            )
            status, _ = imap.append(folder, r"(\Seen)" if rng.random() < 0.8 else None, None, raw)
            if status != "OK":
                raise RuntimeError(f"APPEND to {folder} failed: {status}")
            recent.append((f"<seed-{index}@example.com>", subject))
            if len(recent) > 40:
                recent.pop(0)
            index += 1
        written[folder] = wanted
    imap.logout()
    return written


# --------------------------------------------------------------------------
# Calendars
# --------------------------------------------------------------------------

_ICAL_STAMP = "%Y%m%dT%H%M%S"


def _vevent(uid: str, summary: str, start: datetime, minutes: int, rrule: str | None) -> str:
    """One VEVENT in Europe/Berlin wall-clock time with a TZID, which is what a
    real client writes -- a bare UTC stamp hides every timezone defect."""
    end = start + timedelta(minutes=minutes)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//mail-verdict-seed//EN",
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20260101T000000Z",
        f"DTSTART;TZID=Europe/Berlin:{start.strftime(_ICAL_STAMP)}",
        f"DTEND;TZID=Europe/Berlin:{end.strftime(_ICAL_STAMP)}",
        f"SUMMARY:{summary}",
    ]
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def _vtodo(uid: str, summary: str, due: datetime, done: bool) -> str:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//mail-verdict-seed//EN",
        "BEGIN:VTODO", f"UID:{uid}", "DTSTAMP:20260101T000000Z",
        f"DUE;TZID=Europe/Berlin:{due.strftime(_ICAL_STAMP)}",
        f"SUMMARY:{summary}",
        f"STATUS:{'COMPLETED' if done else 'NEEDS-ACTION'}",
        "END:VTODO", "END:VCALENDAR", "",
    ]
    return "\r\n".join(lines)


def seed_calendars(
    host: str, port: int, *, username: str = DEFAULT_DAV_USER, scale: float = 1.0, seed: int = 11,
) -> dict[str, int]:
    """Event calendars and to-do-only lists, in the proportion a real account
    has them. Returns the object count per kind."""
    rng = _rng(seed)
    now = datetime.now()
    events = todos = 0
    with httpx.Client(auth=(username, "unused"), timeout=30.0) as client:
        principal = discover(client, f"http://{host}:{port}/")
        for calendar_index, name in enumerate(EVENT_CALENDARS):
            url = create_calendar(client, principal, name.lower(), name, ["VEVENT"])
            wanted = max(1, int(EVENTS_PER_CALENDAR * scale))
            for i in range(wanted):
                uid = f"seed-ev-{calendar_index}-{i}"
                # A tenth of them are series that started years ago, which is
                # what makes expansion cost anything at all.
                if i % 10 == 0:
                    start = now.replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(
                        days=rng.randint(400, 2200)
                    )
                    rule = rng.choice(
                        ["FREQ=WEEKLY;BYDAY=MO", "FREQ=MONTHLY;BYMONTHDAY=1", "FREQ=YEARLY",
                         "FREQ=DAILY;INTERVAL=3"]
                    )
                else:
                    start = now + timedelta(
                        days=rng.randint(-540, 540), hours=rng.randint(-6, 8),
                    )
                    start = start.replace(minute=rng.choice((0, 15, 30)), second=0, microsecond=0)
                    rule = None
                put_object(
                    client, f"{url}{uid}.ics",
                    _vevent(uid, f"{name} {i}", start, rng.choice((30, 60, 90, 120)), rule),
                    "text/calendar; charset=utf-8",
                )
                events += 1
        for list_index, name in enumerate(TASK_LISTS):
            url = create_calendar(
                client, principal, f"tasks-{list_index}", name, ["VTODO"],
            )
            wanted = max(1, int(TODOS_PER_LIST * scale))
            for i in range(wanted):
                uid = f"seed-td-{list_index}-{i}"
                put_object(
                    client, f"{url}{uid}.ics",
                    _vtodo(
                        uid, f"{name} item {i}",
                        now + timedelta(days=rng.randint(-200, 200)), i % 3 == 0,
                    ),
                    "text/calendar; charset=utf-8",
                )
                todos += 1
    return {"events": events, "todos": todos, "calendars": len(EVENT_CALENDARS),
            "task_lists": len(TASK_LISTS)}


# --------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------


def _vcard(uid: str, index: int, rng: random.Random, photo: str | None) -> str:
    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    org = rng.choice(ORGANISATIONS)
    groups = rng.sample(CONTACT_GROUPS, k=rng.randint(0, 2))
    lines = [
        "BEGIN:VCARD", "VERSION:3.0", f"UID:{uid}",
        f"FN:{first} {last}", f"N:{last};{first};;;",
        f"EMAIL;TYPE=INTERNET,HOME:{index}.{first.lower()}@example.com",
    ]
    if index % 3 == 0:
        lines.append(f"EMAIL;TYPE=INTERNET,WORK:{first.lower()}.{index}@work.example")
    lines.append(f"TEL;TYPE=CELL:+49 170 {1000000 + index}")
    if org:
        lines += [f"ORG:{org}", f"TITLE:{rng.choice(('Entwicklerin', 'Vorstand', 'Praktikant'))}"]
    if groups:
        lines.append(f"CATEGORIES:{','.join(groups)}")
    if photo:
        lines.append(f"PHOTO;ENCODING=b;TYPE=JPEG:{photo}")
    lines += ["END:VCARD", ""]
    return "\r\n".join(lines)


def _group_card(uid: str, name: str, member_uids: list[str]) -> str:
    lines = ["BEGIN:VCARD", "VERSION:4.0", f"UID:{uid}", "KIND:group", f"FN:{name}"]
    lines += [f"MEMBER:urn:uuid:{member}" for member in member_uids]
    lines += ["END:VCARD", ""]
    return "\r\n".join(lines)


def seed_contacts(
    host: str, port: int, *, username: str = DEFAULT_DAV_USER, count: int = CONTACT_COUNT,
    seed: int = 13,
) -> dict[str, int]:
    """Several address books of contacts, a fifth carrying an embedded photo,
    grouped both ways a real address book groups them."""
    rng = _rng(seed)
    photo = base64.b64encode(bytes(rng.getrandbits(8) for _ in range(PHOTO_BYTES))).decode()
    written = 0
    with httpx.Client(auth=(username, "unused"), timeout=30.0) as client:
        principal = discover(client, f"http://{host}:{port}/")
        books = [
            (name, create_addressbook(client, principal, name.lower(), name))
            for name in ADDRESS_BOOKS
        ]
        uids_by_book: dict[str, list[str]] = {name: [] for name, _ in books}
        for index in range(count):
            name, url = books[index % len(books)]
            uid = f"seed-contact-{index}"
            put_object(
                client, f"{url}{uid}.vcf",
                _vcard(uid, index, rng, photo if rng.random() < PHOTO_FRACTION else None),
                "text/vcard; charset=utf-8",
            )
            uids_by_book[name].append(uid)
            written += 1
        for group_index, group in enumerate(CONTACT_GROUPS):
            name, url = books[group_index % len(books)]
            uid = f"seed-group-{group_index}"
            put_object(
                client, f"{url}{uid}.vcf",
                _group_card(uid, group, uids_by_book[name][: 10 + group_index]),
                "text/vcard; charset=utf-8",
            )
    return {"contacts": written, "address_books": len(books), "groups": len(CONTACT_GROUPS)}


def seed_all(
    *, imap_host: str, imap_port: int, radicale_host: str, radicale_port: int,
    mailbox: str = DEFAULT_MAILBOX, dav_user: str = DEFAULT_DAV_USER, scale: float = 1.0,
) -> dict[str, object]:
    """Everything, in the order a stack wants it: mail first, since it is the
    slowest to sync, then the DAV collections."""
    result: dict[str, object] = {}
    started = time.monotonic()
    result["mail"] = seed_mail(
        imap_host, imap_port, mailbox=mailbox, count=max(10, int(MESSAGE_COUNT * scale)),
    )
    result["mail_seconds"] = round(time.monotonic() - started, 1)

    started = time.monotonic()
    result["calendars"] = seed_calendars(radicale_host, radicale_port, username=dav_user,
                                         scale=scale)
    result["calendar_seconds"] = round(time.monotonic() - started, 1)

    started = time.monotonic()
    result["contacts"] = seed_contacts(
        radicale_host, radicale_port, username=dav_user, count=max(10, int(CONTACT_COUNT * scale)),
    )
    result["contact_seconds"] = round(time.monotonic() - started, 1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--imap-port", type=int, required=True)
    parser.add_argument("--radicale-port", type=int, required=True)
    parser.add_argument("--mailbox", default=DEFAULT_MAILBOX)
    parser.add_argument("--dav-user", default=DEFAULT_DAV_USER)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()

    summary = seed_all(
        imap_host=args.host, imap_port=args.imap_port,
        radicale_host=args.host, radicale_port=args.radicale_port,
        mailbox=args.mailbox, dav_user=args.dav_user, scale=args.scale,
    )
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
