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
