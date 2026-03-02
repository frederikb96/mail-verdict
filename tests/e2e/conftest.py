"""
E2E test fixtures for MailVerdict.

Provides shared fixtures for sending emails via SMTP, checking IMAP,
querying the REST API, and waiting for async sync/verdict operations.

All HTTP clients use IPv4 (127.0.0.1) for podman rootless compatibility.
"""

from __future__ import annotations

import asyncio
import email.utils
import imaplib
import smtplib
import uuid
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.helpers.seed import StalwartSeeder

# Test infrastructure endpoints (host-side ports from compose.test.yaml)
APP_BASE_URL = "http://127.0.0.1:18080"
STALWART_ADMIN_URL = "http://127.0.0.1:8880"
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 1025
IMAP_HOST = "127.0.0.1"
IMAP_PORT = 1143
QDRANT_URL = "http://127.0.0.1:16334"

# Test credentials
ALICE_EMAIL = "alice@test.local"
ALICE_PASSWORD = "testpass123"
SPAMMER_EMAIL = "spammer@test.local"
SPAMMER_PASSWORD = "testpass123"
NEWSLETTER_EMAIL = "newsletter@test.local"
NEWSLETTER_PASSWORD = "testpass123"


@pytest_asyncio.fixture
async def app_client() -> httpx.AsyncClient:
    """Async HTTP client for the MailVerdict REST API."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL,
        transport=transport,
        timeout=30.0,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def qdrant_client() -> httpx.AsyncClient:
    """Async HTTP client for direct Qdrant API access."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=QDRANT_URL,
        transport=transport,
        timeout=10.0,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def stalwart_seeder() -> StalwartSeeder:
    """Stalwart seeder for creating test domains/accounts."""
    seeder = StalwartSeeder(base_url=STALWART_ADMIN_URL)
    async with seeder:
        await seeder.wait_ready()
        yield seeder


def send_email(
    *,
    from_addr: str,
    from_password: str,
    to_addr: str,
    subject: str,
    body: str,
) -> str:
    """
    Send an email via SMTP with authentication.

    Returns the generated Message-ID.
    """
    msg = MIMEText(body, "plain")
    msg_id = f"<{uuid.uuid4()}@test.local>"
    msg["Message-ID"] = msg_id
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(from_addr, from_password)
        smtp.sendmail(from_addr, [to_addr], msg.as_string())

    return msg_id


def imap_get_messages(
    user: str = ALICE_EMAIL,
    password: str = ALICE_PASSWORD,
    folder: str = "INBOX",
) -> list[dict[str, Any]]:
    """
    Fetch message subjects and UIDs from an IMAP folder.

    Returns list of dicts with 'uid', 'subject', and 'flags' keys.
    """
    conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(user, password)
        conn.select(folder)
        _, data = conn.search(None, "ALL")
        if not data or not data[0]:
            return []

        messages = []
        for num in data[0].split():
            _, msg_data = conn.fetch(num, "(UID FLAGS BODY[HEADER.FIELDS (SUBJECT)])")
            if msg_data and msg_data[0]:
                raw = msg_data[0]
                if isinstance(raw, tuple) and len(raw) >= 2:
                    header_bytes = raw[1]
                    info_line = raw[0].decode() if isinstance(raw[0], bytes) else str(raw[0])

                    subject = ""
                    if isinstance(header_bytes, bytes):
                        for line in header_bytes.decode(errors="replace").splitlines():
                            if line.lower().startswith("subject:"):
                                subject = line.split(":", 1)[1].strip()

                    uid = None
                    import re
                    uid_match = re.search(r"UID (\d+)", info_line)
                    if uid_match:
                        uid = int(uid_match.group(1))

                    messages.append({
                        "uid": uid,
                        "subject": subject,
                        "info": info_line,
                    })
        return messages
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def imap_move_message(
    uid: int,
    from_folder: str,
    to_folder: str,
    user: str = ALICE_EMAIL,
    password: str = ALICE_PASSWORD,
) -> bool:
    """Move a message by UID from one folder to another via IMAP COPY+DELETE."""
    conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(user, password)
        conn.select(from_folder)
        result, _ = conn.uid("COPY", str(uid), to_folder)
        if result == "OK":
            conn.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
            conn.expunge()
            return True
        return False
    finally:
        try:
            conn.logout()
        except Exception:
            pass


async def wait_for_new_mail(
    client: httpx.AsyncClient,
    *,
    known_ids: set[str] | None = None,
    subject_contains: str | None = None,
    timeout: int = 120,
    poll_interval: float = 3.0,
) -> dict[str, Any]:
    """
    Poll the /api/mails endpoint until a new mail appears.

    Args:
        client: httpx client targeting the app API
        known_ids: Set of mail IDs already known (to detect new ones)
        subject_contains: Wait for a mail whose subject contains this string
        timeout: Max seconds to wait
        poll_interval: Seconds between polls

    Returns:
        The new mail dict from the API

    Raises:
        TimeoutError: If no new mail appears within timeout
    """
    if known_ids is None:
        known_ids = set()

    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get("/api/mails", params={"limit": 200})
        if resp.status_code == 200:
            mails = resp.json()
            for mail in mails:
                mail_id = mail["id"]
                if mail_id in known_ids:
                    continue
                if subject_contains and (
                    not mail.get("subject")
                    or subject_contains.lower() not in mail["subject"].lower()
                ):
                    continue
                return mail

        await asyncio.sleep(poll_interval)

    raise TimeoutError(
        f"No new mail appeared within {timeout}s"
        + (f" (looking for subject containing '{subject_contains}')" if subject_contains else "")
    )


async def wait_for_verdict(
    client: httpx.AsyncClient,
    mail_id: str,
    *,
    timeout: int = 120,
    poll_interval: float = 3.0,
) -> dict[str, Any]:
    """
    Poll the verdict endpoint until a verdict exists for the given mail.

    Args:
        client: httpx client targeting the app API
        mail_id: UUID string of the mail
        timeout: Max seconds to wait
        poll_interval: Seconds between polls

    Returns:
        The verdict dict from the API

    Raises:
        TimeoutError: If no verdict appears within timeout
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/mails/{mail_id}/verdict")
        if resp.status_code == 200:
            verdict = resp.json()
            if verdict is not None:
                return verdict

        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"No verdict for mail {mail_id} within {timeout}s")


async def get_known_mail_ids(client: httpx.AsyncClient) -> set[str]:
    """Fetch all current mail IDs from the API."""
    resp = await client.get("/api/mails", params={"limit": 200})
    if resp.status_code == 200:
        return {m["id"] for m in resp.json()}
    return set()


async def get_account_id(client: httpx.AsyncClient) -> str:
    """Get the first account's ID from the API."""
    resp = await client.get("/api/accounts")
    assert resp.status_code == 200
    accounts = resp.json()
    assert len(accounts) > 0, "No accounts found"
    return accounts[0]["id"]
