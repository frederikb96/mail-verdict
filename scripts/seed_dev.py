#!/usr/bin/env python3
"""Deliver the test email corpus into the development mail server.

The development stack runs a Dovecot container as a throwaway mail world. Any username
authenticates against one shared password and its mailbox is created on first login, so
there is no account provisioning step -- mail just needs to be delivered.

Delivery goes over LMTP, the same path a real mail transfer agent would use, so the
messages arrive as genuinely inbound mail rather than being injected into the database.

    python scripts/seed_dev.py
    python scripts/seed_dev.py --to bob@test.local --folder INBOX

Run it after the development stack is up. Running it twice delivers the corpus twice --
useful for generating volume, occasionally surprising.

Each delivery is stamped with the current date and a fresh Message-ID, so the corpus
arrives as mail that just landed rather than as mail from the year the fixture was
written. Both matter downstream: the pipeline refuses to classify anything older than
`pipeline.live_max_age_days`, and a repeated Message-ID resolves to a message already
carrying a verdict, so a second run would add messages that are silently never
classified. Pass --keep-dates for the fixtures byte-for-byte.

Alongside the mail corpus, this also seeds a calendar and an address book directly on
the development stack's Radicale server -- CalDAV/CardDAV, spoken with the same "talk to
the throwaway server directly, before any account exists" approach LMTP delivery uses for
mail. Pass --skip-calendar to leave that out. Re-running is idempotent: the same slugs
and UIDs are reused, so a second run overwrites rather than duplicating.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
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
    sample_contact,
    sample_event,
)
from tests.setup.mail_delivery import CORPUS_DIR, load_corpus  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_LMTP_PORT = 24024
DEFAULT_RECIPIENT = "alice@test.local"

DEFAULT_RADICALE_PORT = 15232
DEFAULT_DAV_USER = "alice"

# A handful of events a few days either side of "now", so they land in whatever
# month a developer has the calendar open to right after seeding, plus one
# further out to prove month navigation finds it too.
_ICAL_FORMAT = "%Y%m%dT%H%M%SZ"
_SAMPLE_EVENTS = [
    ("Team standup", timedelta(days=0, hours=9), timedelta(hours=0, minutes=30), None),
    ("Quarterly planning", timedelta(days=2, hours=14), timedelta(hours=2), "Room 4B"),
    ("Dentist appointment", timedelta(days=-1, hours=10), timedelta(hours=1), None),
    ("Project kickoff", timedelta(days=20, hours=11), timedelta(hours=1), "Conference call"),
]
_SAMPLE_CONTACTS = [
    ("Anna Mueller", "anna@example.com"),
    ("Ben Carter", "ben.carter@example.org"),
    ("Chidi Okafor", "chidi@example.net"),
]


class LmtpError(RuntimeError):
    """The mail server rejected something during delivery."""


def _read_reply(stream: object) -> str:
    """Read one LMTP reply, following continuation lines."""
    lines: list[str] = []
    while True:
        raw = stream.readline()  # type: ignore[attr-defined]
        if not raw:
            raise LmtpError("connection closed mid-reply")
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        lines.append(line)
        # A continuation line has a dash in the fourth column; the final one has a space.
        if len(line) < 4 or line[3] != "-":
            return "\n".join(lines)


def deliver(message: bytes, host: str, port: int, sender: str, recipient: str) -> None:
    """Deliver one message over LMTP.

    The development mail server speaks implicit TLS on its LMTP port -- a plain socket
    simply hangs waiting for a greeting that never arrives in the clear. The certificate
    is self-signed, so verification is off; this only ever talks to a local container.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=20) as raw_sock:
        with context.wrap_socket(raw_sock) as sock:
            stream = sock.makefile("rwb")

            greeting = _read_reply(stream)
            if not greeting.startswith("220"):
                raise LmtpError(f"unexpected greeting: {greeting}")

            for command, expected in (
                ("LHLO seed.local", "250"),
                (f"MAIL FROM:<{sender}>", "250"),
                (f"RCPT TO:<{recipient}>", "250"),
                ("DATA", "354"),
            ):
                stream.write(f"{command}\r\n".encode())
                stream.flush()
                reply = _read_reply(stream)
                if not reply.startswith(expected):
                    raise LmtpError(f"{command!r} -> {reply}")

            # Dot-stuffing: a line that is a single dot would otherwise end the message.
            body = message.replace(b"\r\n.\r\n", b"\r\n..\r\n")
            stream.write(body)
            if not body.endswith(b"\r\n"):
                stream.write(b"\r\n")
            stream.write(b".\r\n")
            stream.flush()
            reply = _read_reply(stream)
            if not reply.startswith("250"):
                raise LmtpError(f"message rejected: {reply}")

            stream.write(b"QUIT\r\n")
            stream.flush()


def seed_calendar(host: str, port: int, username: str) -> tuple[str, str]:
    """Seed a calendar and an address book directly on the development Radicale
    server, before any dav_account exists -- the calendar-side counterpart of
    delivering mail over LMTP before an account exists to receive it.

    Returns (calendar_slug, addressbook_slug) so the caller can report what to
    point a DAV account at.
    """
    base_url = f"http://{host}:{port}/"
    now = datetime.now(timezone.utc)
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, base_url)

        calendar_url = create_calendar(client, principal, "personal", "Personal")
        for i, (summary, offset, duration, description) in enumerate(_SAMPLE_EVENTS):
            start = now + offset
            uid = f"seed-event-{i}@mail-verdict.local"
            put_object(
                client, f"{calendar_url}{uid}.ics",
                sample_event(
                    uid, summary,
                    dtstart=start.strftime(_ICAL_FORMAT),
                    dtend=(start + duration).strftime(_ICAL_FORMAT),
                    description=description,
                ),
                "text/calendar; charset=utf-8",
            )

        addressbook_url = create_addressbook(client, principal, "contacts", "Contacts")
        for i, (fn, email) in enumerate(_SAMPLE_CONTACTS):
            uid = f"seed-contact-{i}@mail-verdict.local"
            put_object(
                client, f"{addressbook_url}{uid}.vcf",
                sample_contact(uid, fn, email),
                "text/vcard; charset=utf-8",
            )

    return "personal", "contacts"


def main() -> int:
    """Deliver the corpus and report what landed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST, help="mail server host")
    parser.add_argument(
        "--lmtp-port", type=int, default=DEFAULT_LMTP_PORT, help="LMTP port (implicit TLS)"
    )
    parser.add_argument("--to", default=DEFAULT_RECIPIENT, help="recipient address")
    parser.add_argument(
        "--from", dest="sender", default="sender@example.com", help="envelope sender"
    )
    parser.add_argument(
        "--keep-dates",
        action="store_true",
        help="deliver the fixtures verbatim, keeping their original Date and Message-ID",
    )
    parser.add_argument(
        "--radicale-port", type=int, default=DEFAULT_RADICALE_PORT, help="Radicale HTTP port"
    )
    parser.add_argument(
        "--dav-user", default=DEFAULT_DAV_USER,
        help="username to seed the calendar and address book under",
    )
    parser.add_argument(
        "--skip-calendar", action="store_true", help="skip seeding the calendar and address book"
    )
    args = parser.parse_args()

    corpus = load_corpus(keep_dates=args.keep_dates)
    if not corpus:
        raise SystemExit(f"No .eml files in {CORPUS_DIR}")

    delivered = 0
    for name, message in corpus:
        try:
            deliver(message, args.host, args.lmtp_port, args.sender, args.to)
        except (LmtpError, OSError, ssl.SSLError) as exc:
            print(f"  {name}: FAILED — {exc}", file=sys.stderr)
            continue
        print(f"  {name}")
        delivered += 1

    print(f"\nDelivered {delivered}/{len(corpus)} messages to {args.to}.")

    if delivered == 0:
        print(
            "\nNothing was delivered. Check the development stack is running and that "
            f"the LMTP port is reachable at {args.host}:{args.lmtp_port}.",
            file=sys.stderr,
        )
        return 1

    print(
        "\nAdd the account in the interface, or through the API, using this mailbox as "
        "both the IMAP user and the address. PostIMAP picks up a new account without a "
        "restart and the messages appear as it syncs."
    )

    if not args.skip_calendar:
        try:
            calendar_slug, addressbook_slug = seed_calendar(
                args.host, args.radicale_port, args.dav_user,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            print(f"\nCalendar/address book seeding FAILED — {exc}", file=sys.stderr)
            return 1
        print(
            f"\nSeeded calendar {calendar_slug!r} and address book {addressbook_slug!r} on "
            f"Radicale for user {args.dav_user!r}. Add a DAV account in the interface pointing "
            f"at http://radicale:5232/ (the container's own network alias, not this host's "
            f"{args.radicale_port} port) with username {args.dav_user!r} and any password -- "
            "Radicale accepts them all. PostIMAP backfills both on account creation."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
