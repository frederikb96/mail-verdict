"""
Stalwart mail server seeding for E2E tests.

Creates test domains and accounts via the Stalwart REST management API.
Uses explicit IPv4 (127.0.0.1) for podman rootless compatibility.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8880"
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_SECRET = "testadmin123"
DEFAULT_DOMAIN = "test.local"

TEST_ACCOUNTS = [
    {"email": "alice@test.local", "password": "testpass123", "name": "Alice Test"},
    {"email": "bob@test.local", "password": "testpass123", "name": "Bob Test"},
    {"email": "spammer@test.local", "password": "testpass123", "name": "Spammer Bot"},
    {"email": "newsletter@test.local", "password": "testpass123", "name": "Newsletter Sender"},
]


class StalwartSeeder:
    """
    HTTP client for seeding Stalwart via the management API.

    Handles domain creation, account provisioning, and health polling.
    Use as async context manager to share a single httpx client across calls.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        admin_user: str = DEFAULT_ADMIN_USER,
        admin_secret: str = DEFAULT_ADMIN_SECRET,
    ) -> None:
        """
        Initialize the seeder.

        Args:
            base_url: Stalwart management API base URL (IPv4)
            admin_user: Admin username
            admin_secret: Admin password
        """
        self._base_url = base_url.rstrip("/")
        self._auth = (admin_user, admin_secret)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> StalwartSeeder:
        """Create shared transport and client."""
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        self._client = httpx.AsyncClient(transport=transport, timeout=10.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the shared client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        """Build auth headers for API requests."""
        credentials = f"{self._auth[0]}:{self._auth[1]}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return {
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/json",
        }

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared client, raising if not in context manager."""
        if self._client is None:
            raise RuntimeError("StalwartSeeder must be used as async context manager")
        return self._client

    async def wait_ready(self, timeout: int = 30) -> None:
        """
        Poll until the Stalwart admin API is reachable.

        Args:
            timeout: Maximum seconds to wait

        Raises:
            TimeoutError: If API doesn't respond within timeout
        """
        client = self._get_client()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{self._base_url}/")
                if resp.status_code in (200, 401):
                    logger.info("Stalwart admin API ready")
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(1.0)

        raise TimeoutError(
            f"Stalwart admin API not reachable at {self._base_url} after {timeout}s"
        )

    async def create_domain(self, domain: str) -> bool:
        """
        Create a domain via the principal API.

        Args:
            domain: Domain name (e.g., "test.local")

        Returns:
            True if domain was created or already exists
        """
        client = self._get_client()
        payload: dict[str, Any] = {
            "type": "domain",
            "name": domain,
        }
        resp = await client.post(
            f"{self._base_url}/api/principal",
            headers=self._headers(),
            json=payload,
        )
        if resp.status_code in (200, 201, 204, 409):
            logger.info("Domain created: %s", domain)
            return True
        logger.warning(
            "Failed to create domain %s: %d %s",
            domain, resp.status_code, resp.text,
        )
        return False

    async def create_account(
        self,
        email: str,
        password: str,
        *,
        display_name: str | None = None,
    ) -> bool:
        """
        Create a mail account with full IMAP/SMTP permissions.

        Args:
            email: Full email address (e.g., "alice@test.local")
            password: Account password
            display_name: Optional display name

        Returns:
            True if account was created or already exists
        """
        client = self._get_client()
        payload: dict[str, Any] = {
            "type": "individual",
            "name": email,
            "secrets": [password],
            "emails": [email],
        }
        if display_name:
            payload["description"] = display_name

        resp = await client.post(
            f"{self._base_url}/api/principal",
            headers=self._headers(),
            json=payload,
        )
        if resp.status_code not in (200, 201, 204, 409):
            logger.warning(
                "Failed to create account %s: %d %s",
                email, resp.status_code, resp.text,
            )
            return False

        logger.info("Account created: %s", email)
        await self._grant_mail_permissions(email)
        return True

    async def _grant_mail_permissions(self, username: str) -> None:
        """
        Grant full IMAP/SMTP permissions to an account.

        Stalwart v0.11+ requires explicit permissions for IMAP/email access.

        Args:
            username: Account name to update
        """
        client = self._get_client()
        permissions = [
            "authenticate",
            "email-receive",
            "email-send",
            "imap-authenticate",
            "imap-append",
            "imap-capability",
            "imap-copy",
            "imap-create",
            "imap-delete",
            "imap-enable",
            "imap-expunge",
            "imap-fetch",
            "imap-idle",
            "imap-list",
            "imap-lsub",
            "imap-move",
            "imap-my-rights",
            "imap-namespace",
            "imap-rename",
            "imap-search",
            "imap-select",
            "imap-sort",
            "imap-status",
            "imap-store",
            "imap-subscribe",
            "imap-thread",
        ]
        patch_payload = [
            {"action": "set", "field": "enabledPermissions", "value": permissions},
        ]
        resp = await client.patch(
            f"{self._base_url}/api/principal/{username}",
            headers=self._headers(),
            json=patch_payload,
        )
        if resp.status_code == 200:
            logger.debug("Permissions granted: %s", username)
        else:
            logger.warning(
                "Failed to grant permissions for %s: %d %s",
                username, resp.status_code, resp.text,
            )

    async def delete_all(self) -> None:
        """Delete all test accounts and domain for cleanup."""
        client = self._get_client()
        for account in TEST_ACCOUNTS:
            await client.delete(
                f"{self._base_url}/api/principal/{account['email']}",
                headers=self._headers(),
            )
        await client.delete(
            f"{self._base_url}/api/principal/{DEFAULT_DOMAIN}",
            headers=self._headers(),
        )
        logger.info("Deleted all test accounts and domain")


TEST_EMAILS = [
    {
        "from": "spammer@test.local",
        "subject": "URGENT: Claim your prize now!",
        "body": (
            "You have won $1,000,000! Click here immediately "
            "to claim your prize. Act now or lose forever!"
        ),
    },
    {
        "from": "spammer@test.local",
        "subject": "Limited time offer - 90% discount",
        "body": (
            "Buy cheap pharmaceuticals online. "
            "No prescription needed. Free shipping worldwide."
        ),
    },
    {
        "from": "spammer@test.local",
        "subject": "Your account has been compromised",
        "body": (
            "Dear user, your bank account has been accessed. "
            "Please verify your identity by entering your "
            "password at this link."
        ),
    },
    {
        "from": "newsletter@test.local",
        "subject": "Weekly Python Digest #142",
        "body": (
            "This week in Python:\n"
            "- PEP 750 accepted: Tag Strings\n"
            "- FastAPI 0.115 released\n"
            "- New tutorial: Building async web scrapers"
        ),
    },
    {
        "from": "newsletter@test.local",
        "subject": "Tech News Roundup - March 2026",
        "body": (
            "Top stories:\n"
            "- OpenAI announces GPT-5\n"
            "- EU AI Act enforcement begins\n"
            "- New open-source LLM benchmarks released\n"
            "- Rust adoption in Linux kernel grows"
        ),
    },
    {
        "from": "newsletter@test.local",
        "subject": "Your Monthly Security Brief",
        "body": (
            "Critical vulnerabilities this month:\n"
            "- CVE-2026-1234: RCE in popular library\n"
            "- New phishing campaign targeting cloud providers\n"
            "- Best practices for API key rotation"
        ),
    },
    {
        "from": "alice@test.local",
        "subject": "Meeting notes from standup",
        "body": (
            "Hi team,\n\nKey points from today's standup:\n"
            "- Backend migration on track\n"
            "- Frontend redesign approved\n"
            "- QA starting next week\n\nBest,\nAlice"
        ),
    },
    {
        "from": "alice@test.local",
        "subject": "Re: Project timeline update",
        "body": (
            "Thanks for the update. I agree we should push "
            "the deadline by two weeks to ensure quality. "
            "Let me know if you need anything from my side."
        ),
    },
    {
        "from": "spammer@test.local",
        "subject": "Make $5000/day working from home",
        "body": (
            "Discover the secret that banks don't want you "
            "to know! Our proven system lets you earn thousands "
            "from your couch. Join 50,000 happy customers today!"
        ),
    },
    {
        "from": "newsletter@test.local",
        "subject": "Kubernetes Best Practices 2026",
        "body": (
            "New guide published:\n"
            "- Resource requests vs limits\n"
            "- Pod disruption budgets\n"
            "- GitOps with Flux v2\n"
            "- Monitoring with Prometheus/Grafana"
        ),
    },
    {
        "from": "alice@test.local",
        "subject": "Lunch tomorrow?",
        "body": (
            "Hey! Want to grab lunch tomorrow at that new "
            "vegan place on Pontstrasse? Great bowls."
        ),
    },
    {
        "from": "spammer@test.local",
        "subject": "FINAL WARNING: Account suspension",
        "body": (
            "Your email account will be suspended in 24 hours "
            "unless you verify your identity. Click the link "
            "below to prevent account closure."
        ),
    },
    {
        "from": "newsletter@test.local",
        "subject": "Open Source Friday Highlights",
        "body": (
            "Featured projects this week:\n"
            "- mail-verdict: AI-powered email management\n"
            "- qdrant: Vector similarity search engine\n"
            "- svelte-5: The next generation of Svelte"
        ),
    },
    {
        "from": "alice@test.local",
        "subject": "Code review needed: PR #47",
        "body": (
            "Could you review my PR for the sync engine "
            "refactor? It's a fairly large change but I've "
            "split it into logical commits."
        ),
    },
    {
        "from": "spammer@test.local",
        "subject": "Congratulations! You've been selected",
        "body": (
            "Dear lucky winner, you have been randomly selected "
            "for our exclusive rewards program. Simply provide "
            "your credit card details to receive your gift card."
        ),
    },
    {
        "from": "newsletter@test.local",
        "subject": "Effective Altruism Forum Digest",
        "body": (
            "This week on the EA Forum:\n"
            "- New cause area analysis: AI safety funding\n"
            "- Career advice for aspiring researchers\n"
            "- Update on global health interventions"
        ),
    },
    {
        "from": "alice@test.local",
        "subject": "Vacation request",
        "body": (
            "Hi,\n\nI'd like to take off April 14-18 for a "
            "hiking trip in the Alps. Already coordinated "
            "with the team, no blockers.\n\nThanks!"
        ),
    },
    {
        "from": "spammer@test.local",
        "subject": "RE: Invoice #38291 attached",
        "body": (
            "Please find the attached invoice for services "
            "rendered. Payment is due immediately. "
            "Open the attachment for details."
        ),
    },
]


def send_test_emails(
    smtp_host: str = "127.0.0.1",
    smtp_port: int = 1025,
    to_addr: str = "alice@test.local",
) -> int:
    """
    Send predefined test emails via SMTP for manual testing.

    Args:
        smtp_host: SMTP server host
        smtp_port: SMTP server port
        to_addr: Recipient email address

    Returns:
        Number of emails sent
    """
    import email.utils
    import smtplib
    import uuid as uuid_mod
    from email.mime.text import MIMEText

    sent = 0
    for msg_data in TEST_EMAILS:
        from_addr = msg_data["from"]
        from_password = "testpass123"

        msg = MIMEText(msg_data["body"], "plain")
        msg["Message-ID"] = f"<{uuid_mod.uuid4()}@test.local>"
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = msg_data["subject"]
        msg["Date"] = email.utils.formatdate(localtime=True)

        try:
            with smtplib.SMTP(smtp_host, smtp_port) as smtp:
                smtp.login(from_addr, from_password)
                smtp.sendmail(from_addr, [to_addr], msg.as_string())
            sent += 1
            time.sleep(0.3)
        except Exception as exc:
            logger.warning(
                "Failed to send email '%s': %s",
                msg_data["subject"], exc,
            )

    logger.info("Sent %d/%d test emails to %s", sent, len(TEST_EMAILS), to_addr)
    return sent


async def seed_test_environment(
    base_url: str = DEFAULT_BASE_URL,
    domain: str = DEFAULT_DOMAIN,
) -> StalwartSeeder:
    """
    Seed a complete test environment in Stalwart.

    Creates domain and all predefined test accounts with permissions.

    Args:
        base_url: Stalwart management API URL (IPv4)
        domain: Test domain to create

    Returns:
        Configured StalwartSeeder instance (client already closed after seeding)
    """
    seeder = StalwartSeeder(base_url=base_url)
    async with seeder:
        await seeder.wait_ready()
        await seeder.create_domain(domain)

        for account in TEST_ACCOUNTS:
            await seeder.create_account(
                account["email"],
                account["password"],
                display_name=account["name"],
            )

    return seeder
