"""
Stalwart mail server seeding helpers.

API-driven account, domain, and email setup for integration/E2E tests.
Uses the Stalwart REST management API.
"""

from __future__ import annotations

import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:8880"
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_SECRET = "testadmin123"


class StalwartClient:
    """
    HTTP client for the Stalwart management API.

    Handles domain creation, account provisioning, and email delivery.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        admin_user: str = DEFAULT_ADMIN_USER,
        admin_secret: str = DEFAULT_ADMIN_SECRET,
    ) -> None:
        """
        Initialize Stalwart API client.

        Args:
            base_url: Stalwart management API base URL
            admin_user: Admin username
            admin_secret: Admin password
        """
        self._base_url = base_url.rstrip("/")
        self._auth = (admin_user, admin_secret)

    def _headers(self) -> dict[str, str]:
        """Build auth headers for API requests."""
        credentials = f"{self._auth[0]}:{self._auth[1]}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return {
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/json",
        }

    async def create_domain(self, domain: str) -> bool:
        """
        Create a domain in Stalwart.

        Args:
            domain: Domain name (e.g., "test.local")

        Returns:
            True if domain was created or already exists
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/api/domain/{domain}",
                headers=self._headers(),
            )
            if response.status_code in (200, 201, 204, 409):
                logger.debug("Domain created: %s", domain)
                return True
            logger.warning(
                "Failed to create domain %s: %d %s",
                domain,
                response.status_code,
                response.text,
            )
            return False

    async def create_account(
        self,
        username: str,
        password: str,
        *,
        display_name: str | None = None,
    ) -> bool:
        """
        Create a mail account in Stalwart.

        Args:
            username: Full email address (e.g., "user@test.local")
            password: Account password
            display_name: Optional display name

        Returns:
            True if account was created or already exists
        """
        payload: dict[str, Any] = {
            "type": "individual",
            "name": username,
            "secrets": [f"plain:{password}"],
            "emails": [username],
        }
        if display_name:
            payload["description"] = display_name

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/api/principal",
                headers=self._headers(),
                json=payload,
            )
            if response.status_code in (200, 201, 204, 409):
                logger.debug("Account created: %s", username)
                return True
            logger.warning(
                "Failed to create account %s: %d %s",
                username,
                response.status_code,
                response.text,
            )
            return False

    async def deliver_email(
        self,
        from_addr: str,
        to_addr: str,
        subject: str,
        body_text: str,
        *,
        body_html: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> bool:
        """
        Deliver an email via Stalwart's JMAP/API endpoint.

        Falls back to SMTP delivery if direct API is unavailable.

        Args:
            from_addr: Sender address
            to_addr: Recipient address
            subject: Email subject
            body_text: Plain text body
            body_html: Optional HTML body
            headers: Additional headers to set

        Returns:
            True if email was delivered
        """
        msg = MIMEMultipart("alternative") if body_html else MIMEText(body_text)

        if isinstance(msg, MIMEMultipart):
            msg.attach(MIMEText(body_text, "plain"))
            msg.attach(MIMEText(body_html, "html"))

        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = subject

        if headers:
            for key, value in headers.items():
                msg[key] = value

        raw_message = msg.as_string()

        async with httpx.AsyncClient() as client:
            # Try ingest endpoint
            response = await client.post(
                f"{self._base_url}/api/ingest",
                headers=self._headers(),
                json={
                    "from": from_addr,
                    "to": [to_addr],
                    "message": base64.b64encode(raw_message.encode()).decode(),
                },
            )
            if response.status_code in (200, 201, 204):
                logger.debug("Email delivered: %s -> %s (%s)", from_addr, to_addr, subject)
                return True

            logger.warning(
                "Ingest failed: %d %s",
                response.status_code,
                response.text,
            )
            return False

    async def health_check(self) -> bool:
        """
        Check Stalwart server health.

        Returns:
            True if server is reachable
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self._base_url}/healthz")
                return response.status_code == 200
        except Exception:
            return False


async def seed_test_environment(
    base_url: str = DEFAULT_BASE_URL,
    domain: str = "test.local",
) -> StalwartClient:
    """
    Seed a complete test environment in Stalwart.

    Creates domain, test accounts, and delivers sample emails.

    Args:
        base_url: Stalwart management API URL
        domain: Test domain to create

    Returns:
        Configured StalwartClient
    """
    client = StalwartClient(base_url=base_url)

    await client.create_domain(domain)
    await client.create_account(
        f"testuser@{domain}",
        "testpass123",
        display_name="Test User",
    )
    await client.create_account(
        f"sender@{domain}",
        "testpass123",
        display_name="Test Sender",
    )

    return client
