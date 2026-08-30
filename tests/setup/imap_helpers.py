"""
Raw IMAP helpers against the test Dovecot, using the standard library
imaplib -- no client library beyond what ships with Python.

The IMAP port (31143) is plain, no TLS: confirmed against PostIMAP's own
e2e helpers (imap-helpers.ts, `secure: false`), which talk to the same
image.
"""

from __future__ import annotations

import imaplib
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager

_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")


@contextmanager
def imap_session(host: str, port: int, user: str, password: str) -> Iterator[imaplib.IMAP4]:
    """A logged-in IMAP4 connection, closed and logged out on exit."""
    conn = imaplib.IMAP4(host, port)
    try:
        conn.login(user, password)
        yield conn
    finally:
        try:
            conn.logout()
        except OSError:
            pass


def _fetch_flags(conn: imaplib.IMAP4, sequence_number: bytes) -> set[str]:
    """Fetch the flag set for one message by sequence number."""
    typ, data = conn.fetch(sequence_number.decode(), "(FLAGS)")
    if typ != "OK" or not data or not isinstance(data[0], bytes):
        raise RuntimeError(f"FETCH FLAGS failed for message {sequence_number!r}: {typ} {data!r}")
    match = _FLAGS_RE.search(data[0])
    if match is None:
        return set()
    return {flag.decode() for flag in match.group(1).split()}


def find_message_by_id(conn: imaplib.IMAP4, mailbox: str, message_id: str) -> bytes | None:
    """SEARCH a selected mailbox by Message-ID header, return the match's sequence number."""
    conn.select(mailbox)
    typ, data = conn.search(None, f'(HEADER Message-ID "{message_id}")')
    if typ != "OK" or not data or not data[0]:
        return None
    first: bytes = data[0]
    return first.split()[0]


def wait_for_flags(
    host: str,
    port: int,
    user: str,
    password: str,
    mailbox: str,
    message_id: str,
    expected_flags: set[str],
    timeout_s: float = 20.0,
) -> None:
    """Poll the real IMAP server until a message carries every expected flag.

    Proves a flag change made through the application actually reached the
    mail server, not just the local database row -- PostIMAP applies the
    outbound STORE asynchronously, so this is a genuine wait, not a
    formality.
    """
    deadline = time.monotonic() + timeout_s
    last_seen: set[str] | None = None
    with imap_session(host, port, user, password) as conn:
        while time.monotonic() < deadline:
            seq = find_message_by_id(conn, mailbox, message_id)
            if seq is not None:
                last_seen = _fetch_flags(conn, seq)
                if expected_flags <= last_seen:
                    return
            time.sleep(1)
    raise TimeoutError(
        f"Message {message_id!r} in {mailbox!r} never showed flags {expected_flags} "
        f"on the real IMAP server within {timeout_s}s (last seen: {last_seen})"
    )
