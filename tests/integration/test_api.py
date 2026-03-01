"""
Integration tests: REST API endpoints.

Tests the FastAPI app with httpx AsyncClient against real Postgres and Qdrant.
Uses direct database setup (no IMAP or OpenAI dependencies).

Markers: @pytest.mark.integration
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from mail_verdict.config import (
    AIConfig,
    DatabaseConfig,
    MailVerdictConfig,
    MCPConfig,
    QdrantConfig,
    RetryConfig,
    ServerConfig,
    SpamConfig,
    SyncConfig,
)
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import (
    Account,
    Attachment,
    Base,
    Folder,
    Mail,
    MailTag,
    SpecialUse,
    TagSource,
    Verdict,
    VerdictSource,
)

TEST_DB_URL = (
    "postgresql+asyncpg://mail_verdict_test:mail_verdict_test@localhost:5433/mail_verdict_test"
)
QDRANT_HOST = "localhost"
QDRANT_PORT = 6334

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="module"),
]


def _test_config() -> MailVerdictConfig:
    """Build a minimal config for API testing."""
    return MailVerdictConfig(
        server=ServerConfig(host="127.0.0.1", port=8080, log_level="WARNING", cors_origins=[]),
        accounts=[],
        database=DatabaseConfig(url=TEST_DB_URL, pool_size=5, max_overflow=2),
        qdrant=QdrantConfig(host=QDRANT_HOST, port=QDRANT_PORT, collection_name="test_api"),
        ai=AIConfig(
            provider="openai",
            model="gpt-4o-mini",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        ),
        spam=SpamConfig(
            enabled=False,
            excerpt_length=300,
            neighbor_count=3,
            auto_mark_read=True,
            system_prompt="test",
        ),
        sync=SyncConfig(
            poll_interval_seconds=300,
            idle_enabled=False,
            idle_restart_seconds=1500,
            lookback_days=180,
            auto_detect_folders=True,
        ),
        retry=RetryConfig(
            max_retries=1,
            base_delay_seconds=0.1,
            max_delay_seconds=1.0,
            exponential_base=2.0,
        ),
        mcp=MCPConfig(enabled=False, port=8766, transport="streamable-http"),
        rules=[],
    )


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[DatabaseConnection]:
    """Module-scoped database connection with schema setup."""
    config = DatabaseConfig(url=TEST_DB_URL, pool_size=5, max_overflow=2)
    conn = DatabaseConnection(config)
    await conn.init()
    async with conn.engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield conn
    async with conn.engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
    await conn.close()


@pytest.fixture(scope="module")
async def seed_data(db: DatabaseConnection) -> dict[str, Any]:
    """Seed the database with test data, return IDs."""
    async with db.session() as session:
        acc = Account(
            name="api-test-account",
            imap_host="imap.test.com",
            imap_port=993,
            imap_user="user@test.com",
        )
        session.add(acc)
        await session.flush()

        inbox = Folder(
            account_id=acc.id,
            imap_name="INBOX",
            display_name="Inbox",
            special_use=SpecialUse.INBOX,
        )
        junk = Folder(
            account_id=acc.id,
            imap_name="Junk",
            display_name="Spam",
            special_use=SpecialUse.JUNK,
        )
        session.add(inbox)
        session.add(junk)
        await session.flush()

        mail1 = Mail(
            account_id=acc.id,
            folder_id=inbox.id,
            uid=1,
            subject="Hello World",
            from_addr="sender@example.com",
            to_addrs={"addr": "user@test.com"},
            body_text="Welcome to the test suite.",
            received_at=datetime.now(timezone.utc),
            size_bytes=512,
        )
        mail2 = Mail(
            account_id=acc.id,
            folder_id=inbox.id,
            uid=2,
            subject="Invoice #1234",
            from_addr="billing@company.com",
            body_text="Please find attached your invoice.",
            received_at=datetime.now(timezone.utc),
            is_read=True,
        )
        session.add(mail1)
        session.add(mail2)
        await session.flush()

        att = Attachment(
            mail_id=mail2.id,
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=2048,
        )
        session.add(att)

        verdict = Verdict(
            mail_id=mail1.id,
            is_spam=False,
            source=VerdictSource.AI,
            model_used="gpt-4o-mini",
            reasoning="Legitimate welcome email",
        )
        session.add(verdict)

        tag = MailTag(
            mail_id=mail1.id,
            tag_name="welcome",
            source=TagSource.ENRICHMENT,
        )
        session.add(tag)
        await session.flush()

        return {
            "account_id": acc.id,
            "inbox_id": inbox.id,
            "junk_id": junk.id,
            "mail1_id": mail1.id,
            "mail2_id": mail2.id,
            "verdict_id": verdict.id,
        }


@pytest.fixture(scope="module")
async def client(db: DatabaseConnection, seed_data: dict[str, Any]) -> AsyncIterator[AsyncClient]:
    """Create an httpx AsyncClient with the FastAPI app."""
    from fastapi import FastAPI

    from mail_verdict.api.routes import all_routers
    from mail_verdict.api.verdicts import router as verdicts_router

    # Build a minimal FastAPI app
    app = FastAPI()
    for router in all_routers:
        app.include_router(router)
    app.include_router(verdicts_router)

    # Patch the global db connection to use our test db
    import mail_verdict.database.connection as db_mod

    original = db_mod._db_connection
    db_mod._db_connection = db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    db_mod._db_connection = original


class TestMailEndpoints:
    """Test /api/mails endpoints."""

    async def test_list_mails(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """GET /api/mails returns mail list."""
        resp = await client.get(
            "/api/mails",
            params={"account_id": str(seed_data["account_id"])},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 2

    async def test_list_mails_filter_by_folder(
        self, client: AsyncClient, seed_data: dict[str, Any]
    ) -> None:
        """GET /api/mails with folder_id filter."""
        resp = await client.get(
            "/api/mails",
            params={"folder_id": str(seed_data["inbox_id"])},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(d["folder_id"] == str(seed_data["inbox_id"]) for d in data)

    async def test_list_mails_filter_by_read(
        self, client: AsyncClient, seed_data: dict[str, Any]
    ) -> None:
        """GET /api/mails with is_read filter."""
        resp = await client.get(
            "/api/mails",
            params={"is_read": "true"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(d["is_read"] for d in data)

    async def test_get_mail_detail(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """GET /api/mails/:id returns full detail."""
        resp = await client.get(
            f"/api/mails/{seed_data['mail1_id']}",
            params={"account_id": str(seed_data["account_id"])},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(seed_data["mail1_id"])
        assert data["subject"] == "Hello World"
        assert "tags" in data
        assert "attachments" in data

    async def test_get_mail_not_found(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """GET /api/mails/:id returns 404 for missing mail."""
        fake_id = uuid.uuid4()
        resp = await client.get(
            f"/api/mails/{fake_id}",
            params={"account_id": str(seed_data["account_id"])},
        )
        assert resp.status_code == 404


class TestMailActions:
    """Test /api/mails/:id/action endpoint."""

    async def test_mark_read(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """POST mark_read action."""
        resp = await client.post(
            f"/api/mails/{seed_data['mail1_id']}/action",
            params={"account_id": str(seed_data["account_id"])},
            json={"action": "mark_read"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_mark_unread(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """POST mark_unread action."""
        resp = await client.post(
            f"/api/mails/{seed_data['mail1_id']}/action",
            params={"account_id": str(seed_data["account_id"])},
            json={"action": "mark_unread"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_flag(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """POST flag action."""
        resp = await client.post(
            f"/api/mails/{seed_data['mail1_id']}/action",
            params={"account_id": str(seed_data["account_id"])},
            json={"action": "flag"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_move_to_folder(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """POST move action to Junk folder."""
        resp = await client.post(
            f"/api/mails/{seed_data['mail2_id']}/action",
            params={"account_id": str(seed_data["account_id"])},
            json={"action": "move", "target_folder": "Junk"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "Junk" in (data.get("message") or "")

    async def test_move_bad_folder(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """POST move action with nonexistent folder returns 400."""
        resp = await client.post(
            f"/api/mails/{seed_data['mail1_id']}/action",
            params={"account_id": str(seed_data["account_id"])},
            json={"action": "move", "target_folder": "NonexistentFolder"},
        )
        assert resp.status_code == 400

    async def test_unknown_action(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """POST with unknown action returns 400."""
        resp = await client.post(
            f"/api/mails/{seed_data['mail1_id']}/action",
            params={"account_id": str(seed_data["account_id"])},
            json={"action": "explode"},
        )
        assert resp.status_code == 400


class TestSearchEndpoint:
    """Test /api/search endpoint."""

    async def test_fulltext_search(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """GET /api/search with fulltext mode returns results."""
        resp = await client.get(
            "/api/search",
            params={
                "query": "invoice",
                "mode": "fulltext",
                "account_id": str(seed_data["account_id"]),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "fulltext"
        assert data["query"] == "invoice"
        assert isinstance(data["results"], list)


class TestRuleEndpoints:
    """Test /api/rules endpoints."""

    async def test_list_rules_empty(self, client: AsyncClient) -> None:
        """GET /api/rules returns empty list when no rules configured."""
        resp = await client.get("/api/rules")
        assert resp.status_code == 200
        assert resp.json() == []


class TestVerdictEndpoints:
    """Test /api/verdicts endpoints."""

    async def test_list_verdicts(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """GET /api/verdicts returns verdict list."""
        resp = await client.get("/api/verdicts")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    async def test_list_verdicts_by_mail(
        self, client: AsyncClient, seed_data: dict[str, Any]
    ) -> None:
        """GET /api/verdicts with mail_id filter."""
        resp = await client.get(
            "/api/verdicts",
            params={"mail_id": str(seed_data["mail1_id"])},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(d["mail_id"] == str(seed_data["mail1_id"]) for d in data)

    async def test_get_mail_verdict(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """GET /api/mails/:id/verdict returns latest verdict."""
        resp = await client.get(
            f"/api/mails/{seed_data['mail1_id']}/verdict",
        )
        assert resp.status_code == 200


class TestStatsEndpoint:
    """Test /api/stats endpoint."""

    async def test_get_stats(self, client: AsyncClient, seed_data: dict[str, Any]) -> None:
        """GET /api/stats returns dashboard stats."""
        resp = await client.get(
            "/api/stats",
            params={"account_id": str(seed_data["account_id"])},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "total_mails" in data
        assert "spam_caught" in data
        assert "accuracy" in data
        assert "weekly_trend" in data
        assert "account_sync" in data
