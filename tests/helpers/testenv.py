"""
Test environment management CLI.

Reusable module for setting up, seeding, and inspecting test containers.
All configuration comes from constants defined here (single source of truth).
Agents and scripts import from here — never hardcode ports or credentials.

Usage:
    python -m tests.helpers.testenv seed     # Stalwart accounts + test emails + app accounts
    python -m tests.helpers.testenv reset    # Full teardown + clean + restart
    python -m tests.helpers.testenv inspect  # Show accounts, mail counts, sync status
    python -m tests.helpers.testenv wait     # Wait for all accounts to reach ACTIVE
"""

from __future__ import annotations

import asyncio
import smtplib
import sys
from email.mime.text import MIMEText

import httpx

from tests.helpers.seed import StalwartSeeder

# ─── Single source of truth for test infrastructure ──────────────────
# Host-side ports (mapped in compose.test.yaml)
APP_BASE_URL = "http://127.0.0.1:18080"
STALWART_ADMIN_URL = "http://127.0.0.1:8880"
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 1025
IMAP_HOST = "127.0.0.1"
IMAP_PORT = 1143

# Container-internal hostnames (for app → stalwart communication)
STALWART_INTERNAL_HOST = "stalwart"
STALWART_INTERNAL_IMAP_PORT = 1143
STALWART_INTERNAL_SMTP_PORT = 2525

# Test credentials
ALICE_EMAIL = "alice@test.local"
ALICE_PASSWORD = "testpass123"
BOB_EMAIL = "bob@test.local"
BOB_PASSWORD = "testpass123"
SPAMMER_EMAIL = "spammer@test.local"
SPAMMER_PASSWORD = "testpass123"
TEST_DOMAIN = "test.local"

# Compose file
COMPOSE_FILE = "compose.test.yaml"
APP_CONTAINER = "mv-app-test"


def send_email(
    *,
    from_addr: str,
    from_password: str,
    to_addr: str,
    subject: str,
    body: str,
) -> None:
    """Send a test email via SMTP (host-side port)."""
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.login(from_addr, from_password)
        server.send_message(msg)


async def wait_healthy(timeout: int = 60) -> None:
    """Wait for the app to be healthy."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=10.0,
    ) as client:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get("/api/health")
                if resp.status_code == 200:
                    print(f"App healthy: {resp.json()}")
                    return
            except (httpx.ConnectError, httpx.ReadError):
                pass
            await asyncio.sleep(1)
    raise TimeoutError(f"App not healthy after {timeout}s")


async def seed_stalwart() -> None:
    """Create test domain and accounts in Stalwart."""
    seeder = StalwartSeeder(base_url=STALWART_ADMIN_URL)
    async with seeder:
        await seeder.wait_ready()
        await seeder.create_domain(TEST_DOMAIN)
        for email, password, name in [
            (ALICE_EMAIL, ALICE_PASSWORD, "Alice Test"),
            (BOB_EMAIL, BOB_PASSWORD, "Bob Test"),
            (SPAMMER_EMAIL, SPAMMER_PASSWORD, "Spammer Bot"),
            ("newsletter@test.local", "testpass123", "Newsletter Sender"),
        ]:
            await seeder.create_account(email, password, display_name=name)
    print("Stalwart seeded with domain + 3 accounts")


async def create_app_account(name: str, email: str, password: str) -> str:
    """Create a mail account in MailVerdict. Returns account ID."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        # Skip if exists
        resp = await client.get("/api/accounts")
        for acct in resp.json():
            if acct["name"] == name:
                print(f"Account '{name}' already exists: {acct['id']}")
                return str(acct["id"])

        resp = await client.post("/api/accounts", json={
            "name": name,
            "imap_host": STALWART_INTERNAL_HOST,
            "imap_port": STALWART_INTERNAL_IMAP_PORT,
            "imap_user": email,
            "imap_password": password,
            "smtp_host": STALWART_INTERNAL_HOST,
            "smtp_port": STALWART_INTERNAL_SMTP_PORT,
            "smtp_user": email,
            "smtp_password": password,
        })
        assert resp.status_code == 201, f"Failed to create {name}: {resp.text}"
        account_id = resp.json()["id"]
        print(f"Account '{name}' created: {account_id}")
        return str(account_id)


def send_seed_emails() -> int:
    """Send test emails using the canonical set from tests.helpers.seed."""
    from tests.helpers.seed import send_test_emails

    count = send_test_emails(
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        to_addr=ALICE_EMAIL,
    )
    print(f"Sent {count} seed emails")
    return count


async def wait_accounts_active(timeout: int = 60) -> None:
    """Wait for all accounts to reach ACTIVE state (set by PostIMAP after sync)."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        resp = await client.get("/api/accounts")
        accounts = resp.json()
        for acct in accounts:
            aid = acct["id"]
            name = acct["name"]
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                r = await client.get(f"/api/accounts/{aid}")
                state = r.json().get("state", "")
                if state == "active":
                    print(f"  {name}: ACTIVE")
                    break
                if state == "error":
                    print(f"  {name}: ERROR")
                    break
                await asyncio.sleep(1)
            else:
                print(f"  {name}: TIMEOUT (last state: {state})")


async def inspect() -> None:
    """Show current test environment state."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=10.0,
    ) as client:
        try:
            health = await client.get("/api/health")
            print(f"Health: {health.json()}")
        except Exception as e:
            print(f"App unreachable: {e}")
            return

        accounts = (await client.get("/api/accounts")).json()
        print(f"\nAccounts ({len(accounts)}):")
        for acct in accounts:
            aid = acct["id"]
            msgs = (await client.get(f"/api/mails?account_id={aid}")).json()
            folders = (await client.get(f"/api/accounts/{aid}/folders")).json()
            print(f"  {acct['name']}: state={acct['state']}, "
                  f"folders={len(folders)}, messages={len(msgs.get('messages', []))}")


async def full_seed() -> dict[str, str]:
    """Complete seed: Stalwart + accounts + emails + restart + wait for PostIMAP sync."""
    await wait_healthy()
    await seed_stalwart()
    alice_id = await create_app_account("alice", ALICE_EMAIL, ALICE_PASSWORD)
    bob_id = await create_app_account("bob", BOB_EMAIL, BOB_PASSWORD)
    send_seed_emails()

    # PostIMAP auto-detects new accounts via PG NOTIFY — no restart needed
    # Wait for PostIMAP to sync accounts to ACTIVE state
    print("Waiting for PostIMAP to sync accounts...")
    await wait_accounts_active(timeout=120)
    return {"alice_id": alice_id, "bob_id": bob_id}


async def full_reset() -> None:
    """Teardown + clean data + restart fresh."""
    import subprocess
    subprocess.run(["podman", "compose", "-f", COMPOSE_FILE, "down"], check=True)
    subprocess.run(
        ["podman", "unshare", "rm", "-rf",
         "/tmp/mv-test-pgdata", "/tmp/mv-test-qdrant", "/tmp/mv-test-stalwart"],
        check=True,
    )
    subprocess.run(["podman", "compose", "-f", COMPOSE_FILE, "up", "-d"], check=True)
    print("Containers restarted with clean data")
    await wait_healthy(timeout=120)


async def _main(cmd: str) -> None:
    """CLI dispatcher."""
    if cmd == "seed":
        await full_seed()
    elif cmd == "reset":
        await full_reset()
    elif cmd == "inspect":
        await inspect()
    elif cmd == "wait":
        await wait_healthy()
        await wait_accounts_active()
    elif cmd == "reset-seed":
        await full_reset()
        await full_seed()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python -m tests.helpers.testenv [seed|reset|inspect|wait|reset-seed]")
        sys.exit(1)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "inspect"
    asyncio.run(_main(cmd))
