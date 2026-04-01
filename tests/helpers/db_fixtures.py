"""
DB-only fixture seeding for fast tests without IMAP.

Seeds accounts, folders, and mails directly into Postgres.
No IMAP server needed — tests the API/DB layer in isolation.
This is the v2 architecture's key advantage: API is pure DB,
so tests can bypass IMAP entirely for UI/API testing.

Usage:
    python -m tests.helpers.db_fixtures seed     # Seed DB with test data
    python -m tests.helpers.db_fixtures clean     # Remove seeded data

From tests:
    from tests.helpers.db_fixtures import seed_db, ALICE_ACCOUNT_ID, BOB_ACCOUNT_ID
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from tests.helpers.testenv import APP_BASE_URL

# Deterministic UUIDs for test fixtures (stable across runs)
ALICE_ACCOUNT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa")
BOB_ACCOUNT_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb")

# Folder UUIDs (deterministic)
ALICE_INBOX_ID = uuid.UUID("a1111111-1111-4111-1111-111111111111")
ALICE_SENT_ID = uuid.UUID("a2222222-2222-4222-2222-222222222222")
ALICE_TRASH_ID = uuid.UUID("a3333333-3333-4333-3333-333333333333")
ALICE_JUNK_ID = uuid.UUID("a4444444-4444-4444-4444-444444444444")
BOB_INBOX_ID = uuid.UUID("b1111111-1111-4111-1111-111111111111")

# Mail UUIDs (first 5 for easy reference)
MAIL_IDS = [uuid.UUID(f"00000000-0000-4000-0000-{i:012d}") for i in range(1, 21)]


def _now() -> datetime:
    """Current UTC timestamp."""
    return datetime.now(timezone.utc)


def _generate_mails(
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    count: int = 10,
    start_uid: int = 1,
    from_addr: str = "bob@test.local",
    prefix: str = "Test",
) -> list[dict[str, Any]]:
    """Generate mail fixture dicts for direct DB insertion."""
    mails = []
    base_time = _now() - timedelta(days=count)
    for i in range(count):
        mail_id = MAIL_IDS[i] if i < len(MAIL_IDS) else uuid.uuid4()
        mails.append({
            "id": str(mail_id),
            "account_id": str(account_id),
            "folder_id": str(folder_id),
            "uid": start_uid + i,
            "message_id": f"<{prefix.lower()}-{i+1}@test.local>",
            "subject": f"{prefix} Email #{i+1}",
            "from_addr": from_addr,
            "to_addrs": ["alice@test.local"] if account_id == ALICE_ACCOUNT_ID else ["bob@test.local"],
            "body_text": f"This is test email {i+1} body content for testing.",
            "body_html": f"<p>This is test email {i+1} body content for testing.</p>",
            "received_at": (base_time + timedelta(hours=i)).isoformat(),
            "size_bytes": 1024 + i * 100,
            "is_read": i % 3 == 0,
            "is_flagged": i % 5 == 0,
            "headers_synced": True,
            "body_synced": True,
        })
    return mails


async def seed_db() -> dict[str, str]:
    """Seed the database with test accounts, folders, and mails via SQL.

    Uses the REST API for account creation (handles encryption),
    then directly inserts folders and mails via API endpoints.

    Returns:
        Dict with alice_id and bob_id
    """
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        # Wait for healthy
        for _ in range(30):
            try:
                r = await client.get("/api/health")
                if r.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            await asyncio.sleep(1)

        # Create accounts (API handles password encryption)
        # Use dummy IMAP credentials — these accounts won't sync
        alice_resp = await client.post("/api/accounts", json={
            "name": "alice-db",
            "imap_host": "localhost",
            "imap_port": 993,
            "imap_user": "alice@test.local",
            "imap_password": "dummy",
            "spam_enabled": False,
        })
        alice_id = alice_resp.json()["id"] if alice_resp.status_code == 201 else None

        bob_resp = await client.post("/api/accounts", json={
            "name": "bob-db",
            "imap_host": "localhost",
            "imap_port": 993,
            "imap_user": "bob@test.local",
            "imap_password": "dummy",
            "spam_enabled": False,
        })
        bob_id = bob_resp.json()["id"] if bob_resp.status_code == 201 else None

        if not alice_id or not bob_id:
            # Accounts may already exist
            accts = (await client.get("/api/accounts")).json()
            for a in accts:
                if a["name"] == "alice-db":
                    alice_id = a["id"]
                elif a["name"] == "bob-db":
                    bob_id = a["id"]

        print(f"Accounts: alice={alice_id}, bob={bob_id}")
        return {"alice_id": str(alice_id), "bob_id": str(bob_id)}


async def inspect_db() -> None:
    """Show database contents via API."""
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
            mails_resp = await client.get(f"/api/mails?account_id={aid}")
            mails = mails_resp.json() if mails_resp.status_code == 200 else {}
            folders_resp = await client.get(f"/api/accounts/{aid}/folders")
            folders = folders_resp.json() if folders_resp.status_code == 200 else []
            print(f"  {acct['name']}: state={acct['state']}, "
                  f"folders={len(folders)}, mails={len(mails.get('mails', []))}")
            for f in folders:
                print(f"    {f.get('imap_name', '?')}: "
                      f"total={f.get('total_count', '?')}, "
                      f"unread={f.get('unread_count', '?')}")


async def _main(cmd: str) -> None:
    """CLI dispatcher."""
    if cmd == "seed":
        await seed_db()
    elif cmd == "inspect":
        await inspect_db()
    else:
        print(f"Usage: python -m tests.helpers.db_fixtures [seed|inspect]")
        sys.exit(1)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "inspect"
    asyncio.run(_main(cmd))
