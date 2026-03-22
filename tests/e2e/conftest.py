"""
E2E test fixtures for MailVerdict.

Provides shared fixtures for sending emails via SMTP, checking IMAP,
querying the REST API, and waiting for async sync/verdict operations.

Seeding: via API (POST /api/accounts) + Stalwart admin API.
All HTTP clients use IPv4 (127.0.0.1) for podman rootless compatibility.
"""

from __future__ import annotations

import asyncio
import email.utils
import imaplib
import logging
import os
import smtplib
import uuid
from email.mime.text import MIMEText
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.helpers.seed import StalwartSeeder

logger = logging.getLogger(__name__)

# Test infrastructure endpoints (host-side ports from compose.test.yaml)
APP_BASE_URL = "http://127.0.0.1:18080"
STALWART_ADMIN_URL = "http://127.0.0.1:8880"
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 1025
IMAP_HOST = "127.0.0.1"
IMAP_PORT = 1143
QDRANT_URL = "http://127.0.0.1:16334"
QDRANT_COLLECTION = "mail_embeddings"

# Test credentials
ALICE_EMAIL = "alice@test.local"
ALICE_PASSWORD = "testpass123"
BOB_EMAIL = "bob@test.local"
BOB_PASSWORD = "testpass123"
SPAMMER_EMAIL = "spammer@test.local"
SPAMMER_PASSWORD = "testpass123"
NEWSLETTER_EMAIL = "newsletter@test.local"
NEWSLETTER_PASSWORD = "testpass123"


async def _wait_healthy(client: httpx.AsyncClient, timeout: int = 60) -> None:
    """Poll health endpoint until healthy or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = await client.get("/api/health")
            if resp.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
            pass
        await asyncio.sleep(1)
    raise TimeoutError(f"App not healthy after {timeout}s")


async def _seed_stalwart() -> None:
    """Seed Stalwart with test domain and accounts (Alice, Bob, Spammer, Newsletter)."""
    seeder = StalwartSeeder(base_url=STALWART_ADMIN_URL)
    async with seeder:
        await seeder.wait_ready()
        await seeder.create_domain("test.local")
        for acct in [
            {"email": ALICE_EMAIL, "password": ALICE_PASSWORD, "name": "Alice Test"},
            {"email": BOB_EMAIL, "password": BOB_PASSWORD, "name": "Bob Test"},
            {"email": SPAMMER_EMAIL, "password": SPAMMER_PASSWORD, "name": "Spammer Bot"},
            {"email": NEWSLETTER_EMAIL, "password": NEWSLETTER_PASSWORD, "name": "Newsletter"},
        ]:
            await seeder.create_account(acct["email"], acct["password"], display_name=acct["name"])


async def _seed_app_account(
    client: httpx.AsyncClient,
    *,
    name: str,
    email_addr: str,
    password: str,
    spam_enabled: bool = True,
) -> str:
    """Register a mail account in MailVerdict via API. Returns account ID.

    Skips creation if an account with the given name already exists.

    Args:
        client: HTTP client pointed at the MailVerdict API
        name: Account display name (e.g. "alice", "bob")
        email_addr: IMAP/SMTP login email
        password: IMAP/SMTP login password
        spam_enabled: Whether to enable spam verdicts
    """
    resp = await client.get("/api/accounts")
    accounts = resp.json()
    for acct in accounts:
        if acct["name"] == name:
            return str(acct["id"])

    resp = await client.post("/api/accounts", json={
        "name": name,
        "imap_host": "stalwart",
        "imap_port": 1143,
        "imap_user": email_addr,
        "imap_password": password,
        "smtp_host": "stalwart",
        "smtp_port": 2525,
        "smtp_user": email_addr,
        "smtp_password": password,
        "spam_enabled": spam_enabled,
    })
    assert resp.status_code == 201, f"Account creation failed for {name}: {resp.text}"
    return str(resp.json()["id"])


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async fixtures."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _restart_app() -> None:
    """Restart the app container so sync engine picks up new accounts."""
    proc = await asyncio.create_subprocess_exec(
        "podman", "restart", "mv-app-test",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    # Poll for healthy immediately (no hardcoded sleep)
    fresh_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=fresh_transport, timeout=30.0,
    ) as fresh_client:
        await _wait_healthy(fresh_client, timeout=120)


def _send_seed_emails() -> int:
    """Send test emails between Alice and Bob via SMTP from the host.

    Sends a mix of Alice->Bob and Bob->Alice emails so both inboxes
    have content for sync testing.

    Returns:
        Number of emails successfully sent.
    """
    import time

    seed_emails = [
        {"from": ALICE_EMAIL, "from_pw": ALICE_PASSWORD, "to": BOB_EMAIL,
         "subject": "Hey Bob, welcome aboard!",
         "body": (
             "Hi Bob,\n\nWelcome to the team. "
             "Let me know if you need anything.\n\nBest,\nAlice"
         )},
        {"from": BOB_EMAIL, "from_pw": BOB_PASSWORD, "to": ALICE_EMAIL,
         "subject": "Re: Hey Bob, welcome aboard!",
         "body": (
             "Thanks Alice! Happy to be here. "
             "Looking forward to working together.\n\nBob"
         )},
        {"from": ALICE_EMAIL, "from_pw": ALICE_PASSWORD, "to": BOB_EMAIL,
         "subject": "Project kickoff meeting",
         "body": (
             "Bob,\n\nLet's schedule a kickoff for next Monday "
             "at 10am.\nI'll send a calendar invite.\n\nAlice"
         )},
        {"from": BOB_EMAIL, "from_pw": BOB_PASSWORD, "to": ALICE_EMAIL,
         "subject": "Re: Project kickoff meeting",
         "body": (
             "Monday 10am works for me. "
             "Should I prepare anything?\n\nBob"
         )},
        {"from": ALICE_EMAIL, "from_pw": ALICE_PASSWORD, "to": BOB_EMAIL,
         "subject": "Code review request",
         "body": (
             "Hey Bob, could you review PR #12 "
             "when you get a chance? No rush.\n\nAlice"
         )},
        {"from": BOB_EMAIL, "from_pw": BOB_PASSWORD, "to": ALICE_EMAIL,
         "subject": "Lunch plans",
         "body": (
             "Alice, want to grab lunch at the new place "
             "on Pontstrasse?\n\nBob"
         )},
        {"from": ALICE_EMAIL, "from_pw": ALICE_PASSWORD, "to": BOB_EMAIL,
         "subject": "Re: Lunch plans",
         "body": "Sounds great! 12:30 works for me.\n\nAlice"},
        {"from": BOB_EMAIL, "from_pw": BOB_PASSWORD, "to": ALICE_EMAIL,
         "subject": "Bug report: sync stalls on large folders",
         "body": (
             "I noticed the sync seems to stall when a folder "
             "has >1000 messages.\nCan you take a look?\n\nBob"
         )},
        {"from": ALICE_EMAIL, "from_pw": ALICE_PASSWORD, "to": BOB_EMAIL,
         "subject": "Team standup notes",
         "body": "Quick update from today's standup: deployment on track.\n\nAlice"},
        {"from": BOB_EMAIL, "from_pw": BOB_PASSWORD, "to": ALICE_EMAIL,
         "subject": "Re: Team standup notes",
         "body": "Got it. I'll finish the API tests today.\n\nBob"},
        {"from": ALICE_EMAIL, "from_pw": ALICE_PASSWORD, "to": BOB_EMAIL,
         "subject": "Security review findings",
         "body": "Found a few issues in the auth module. Details in PR #15.\n\nAlice"},
        {"from": BOB_EMAIL, "from_pw": BOB_PASSWORD, "to": ALICE_EMAIL,
         "subject": "Re: Security review findings",
         "body": "I'll address those comments by EOD.\n\nBob"},
    ]

    sent = 0
    for msg_data in seed_emails:
        try:
            send_email(
                from_addr=msg_data["from"],
                from_password=msg_data["from_pw"],
                to_addr=msg_data["to"],
                subject=msg_data["subject"],
                body=msg_data["body"],
            )
            sent += 1
            time.sleep(0.1)
        except Exception as exc:
            logger.warning("Failed to send seed email '%s': %s", msg_data["subject"], exc)

    logger.info("Sent %d/%d seed emails between Alice and Bob", sent, len(seed_emails))
    return sent


async def _wait_for_account_active(
    client: httpx.AsyncClient,
    account_id: str,
    *,
    timeout: int = 60,
    poll_interval: float = 1.0,
) -> str:
    """Poll account state until it reaches ACTIVE (or ERROR).

    Args:
        client: HTTP client pointed at the MailVerdict API
        account_id: Account UUID to poll
        timeout: Maximum seconds to wait
        poll_interval: Seconds between polls

    Returns:
        Final account state string

    Raises:
        TimeoutError: If account doesn't reach ACTIVE within timeout
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/accounts/{account_id}")
        if resp.status_code == 200:
            state = resp.json().get("state", "")
            if state == "active":
                logger.info("Account %s reached ACTIVE state", account_id[:8])
                return state
            if state == "error":
                logger.warning("Account %s entered ERROR state", account_id[:8])
                return state
        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"Account {account_id[:8]} did not reach ACTIVE within {timeout}s")


@pytest_asyncio.fixture(scope="session")
async def seeded_env() -> dict[str, str]:
    """Session-scoped seed: Stalwart + app accounts (Alice, Bob) via API, then restart for sync."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")

    # Phase 1: seed Stalwart + create accounts + configure settings via API
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        await _wait_healthy(client)
        # Set OpenAI API key via Settings API (not env var)
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        assert openai_key, "OPENAI_API_KEY env var required for E2E tests"
        await client.put("/api/settings/ai", json={
            "data": {"api_key": openai_key},
        })
        # Set fast sync interval for tests (10s instead of 300s default)
        await client.put("/api/settings/sync", json={
            "data": {"poll_interval_seconds": 10, "idle_enabled": False},
        })
        await _seed_stalwart()
        alice_id = await _seed_app_account(
            client, name="alice", email_addr=ALICE_EMAIL, password=ALICE_PASSWORD,
        )
        bob_id = await _seed_app_account(
            client, name="bob", email_addr=BOB_EMAIL, password=BOB_PASSWORD,
        )
    # Client closed here before restart

    # Phase 2: send test emails between Alice and Bob (from host via SMTP)
    _send_seed_emails()

    # Phase 3: restart app so SyncEngine picks up both accounts from DB
    await _restart_app()
    logger.info("App restarted after account seeding")

    # Phase 4: wait for both accounts to reach ACTIVE
    fresh_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=fresh_transport, timeout=30.0,
    ) as client:
        await _wait_healthy(client)
        alice_state = await _wait_for_account_active(client, alice_id)
        bob_state = await _wait_for_account_active(client, bob_id)
        assert alice_state == "active", f"Alice stuck in state: {alice_state}"
        assert bob_state == "active", f"Bob stuck in state: {bob_state}"

    return {"account_id": alice_id, "alice_id": alice_id, "bob_id": bob_id}


@pytest_asyncio.fixture
async def app_client(seeded_env: dict[str, str]) -> httpx.AsyncClient:
    """Async HTTP client for the MailVerdict REST API."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def qdrant_client() -> httpx.AsyncClient:
    """Async HTTP client for direct Qdrant API access."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=QDRANT_URL, transport=transport, timeout=10.0,
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
    max_retries: int = 5,
    retry_delay: float = 10.0,
) -> str:
    """Send an email via SMTP with authentication. Returns Message-ID.

    Retries on rate limit errors (452) from the test mail server.
    """
    import time

    msg = MIMEText(body, "plain")
    msg_id = f"<{uuid.uuid4()}@test.local>"
    msg["Message-ID"] = msg_id
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)

    for attempt in range(max_retries):
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
                smtp.login(from_addr, from_password)
                smtp.sendmail(from_addr, [to_addr], msg.as_string())
            return msg_id
        except smtplib.SMTPRecipientsRefused as exc:
            errors = list(exc.recipients.values())
            if errors and errors[0][0] == 452 and attempt < max_retries - 1:
                logger.warning(
                    "SMTP rate limit hit, retrying in %.0fs (attempt %d/%d)",
                    retry_delay, attempt + 1, max_retries,
                )
                time.sleep(retry_delay)
                continue
            raise

    return msg_id


def _imap_quote(folder: str) -> str:
    """Quote an IMAP folder name if it contains spaces."""
    if " " in folder and not folder.startswith('"'):
        return f'"{folder}"'
    return folder


def imap_get_messages(
    user: str = ALICE_EMAIL,
    password: str = ALICE_PASSWORD,
    folder: str = "INBOX",
) -> list[dict[str, Any]]:
    """Fetch message subjects and UIDs from an IMAP folder."""
    conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(user, password)
        conn.select(_imap_quote(folder))
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
        conn.select(_imap_quote(from_folder))
        result, _ = conn.uid("COPY", str(uid), _imap_quote(to_folder))
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
    account_id: str | None = None,
    timeout: int = 60,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """Poll /api/mails until a new mail appears."""
    if known_ids is None:
        known_ids = set()

    deadline = asyncio.get_event_loop().time() + timeout
    params: dict[str, Any] = {"limit": 200}
    if account_id:
        params["account_id"] = account_id

    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get("/api/mails", params=params)
        if resp.status_code == 200:
            data = resp.json()
            mails = data.get("mails", data) if isinstance(data, dict) else data
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
    timeout: int = 60,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """Poll the verdict endpoint until a verdict exists for the given mail."""
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/mails/{mail_id}/verdict")
        if resp.status_code == 200:
            verdict = resp.json()
            if verdict is not None:
                return verdict

        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"No verdict for mail {mail_id} within {timeout}s")


async def get_known_mail_ids(
    client: httpx.AsyncClient,
    account_id: str | None = None,
) -> set[str]:
    """Fetch all current mail IDs from the API."""
    params: dict[str, Any] = {"limit": 200}
    if account_id:
        params["account_id"] = account_id
    resp = await client.get("/api/mails", params=params)
    if resp.status_code == 200:
        data = resp.json()
        mails = data.get("mails", data) if isinstance(data, dict) else data
        return {m["id"] for m in mails}
    return set()


async def get_account_id(
    client: httpx.AsyncClient,
    name: str = "alice",
) -> str:
    """Get an account ID by name (defaults to 'alice')."""
    resp = await client.get("/api/accounts")
    assert resp.status_code == 200
    accounts = resp.json()
    assert len(accounts) > 0, "No accounts found"
    for acct in accounts:
        if acct["name"] == name:
            return acct["id"]
    return accounts[0]["id"]
