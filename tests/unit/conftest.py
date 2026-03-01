"""
Unit test conftest: mocked DB, mocked Qdrant, mocked OpenAI.

All external dependencies are fully mocked for pure unit testing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.config import (
    AccountConfig,
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
from mail_verdict.rules.bus import EventBus
from mail_verdict.rules.conditions import MailContext
from mail_verdict.sync.events import (
    FlagsChanged,
    MailDeleted,
    MailMoved,
    MailReceived,
    MailSpamDetected,
    MailTrashed,
    SyncEvent,
)

# ---------------------------------------------------------------------------
# Config fixtures (unit-safe, no file I/O needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def test_config() -> MailVerdictConfig:
    """Complete test configuration with sensible defaults."""
    return MailVerdictConfig(
        server=ServerConfig(host="127.0.0.1", port=8080, log_level="DEBUG", cors_origins=[]),
        accounts=[
            AccountConfig(
                name="test-account",
                host="imap.test.local",
                port=993,
                username="testuser@test.local",
                password="testpass",
                folders=["INBOX", "Sent", "Trash"],
                idle_folders=["INBOX"],
                smtp_host="smtp.test.local",
                smtp_port=465,
            ),
        ],
        database=DatabaseConfig(
            url="postgresql+asyncpg://test:test@localhost:5432/test",
            pool_size=5,
            max_overflow=2,
        ),
        qdrant=QdrantConfig(
            host="localhost",
            port=6333,
            collection_name="test_embeddings",
        ),
        ai=AIConfig(
            provider="openai",
            model="gpt-4o-mini",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        ),
        spam=SpamConfig(
            enabled=True,
            excerpt_length=300,
            neighbor_count=3,
            auto_mark_read=True,
            system_prompt="Test spam prompt",
        ),
        sync=SyncConfig(
            poll_interval_seconds=60,
            idle_enabled=True,
            idle_restart_seconds=300,
            lookback_days=30,
            auto_detect_folders=True,
        ),
        retry=RetryConfig(
            max_retries=2,
            base_delay_seconds=0.01,
            max_delay_seconds=0.1,
            exponential_base=2.0,
        ),
        mcp=MCPConfig(enabled=False, port=8766, transport="streamable-http"),
        rules=[],
    )


@pytest.fixture
def disabled_spam_config() -> MailVerdictConfig:
    """Config with spam detection disabled."""
    return MailVerdictConfig(
        server=ServerConfig(host="127.0.0.1", port=8080, log_level="DEBUG", cors_origins=[]),
        accounts=[],
        database=DatabaseConfig(
            url="postgresql+asyncpg://test:test@localhost:5432/test",
            pool_size=5,
            max_overflow=2,
        ),
        qdrant=QdrantConfig(host="localhost", port=6333, collection_name="test"),
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
            system_prompt="",
        ),
        sync=SyncConfig(
            poll_interval_seconds=60,
            idle_enabled=True,
            idle_restart_seconds=300,
            lookback_days=30,
            auto_detect_folders=True,
        ),
        retry=RetryConfig(
            max_retries=2,
            base_delay_seconds=0.01,
            max_delay_seconds=0.1,
            exponential_base=2.0,
        ),
        mcp=MCPConfig(enabled=False, port=8766, transport="streamable-http"),
        rules=[],
    )


# ---------------------------------------------------------------------------
# Event bus fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus() -> EventBus:
    """Fresh event bus for testing."""
    return EventBus()


# ---------------------------------------------------------------------------
# Mail context factory
# ---------------------------------------------------------------------------


@pytest.fixture
def make_mail_context():
    """
    Factory for creating MailContext with overrides.

    Usage: ctx = make_mail_context(subject="Test", from_addr="a@b.com")
    """

    def _factory(**kwargs: Any) -> MailContext:
        defaults: dict[str, Any] = {
            "subject": "Test Subject",
            "body_text": "Test body content",
            "body_html": "",
            "from_addr": "sender@example.com",
            "to_addrs": ["recipient@example.com"],
            "cc_addrs": [],
            "raw_headers": {},
            "size_bytes": 1024,
            "has_attachments": False,
            "attachment_types": [],
            "folder": "INBOX",
            "tags": [],
            "enrichment_tags": [],
        }
        defaults.update(kwargs)
        return MailContext(**defaults)

    return _factory


# ---------------------------------------------------------------------------
# Event factories
# ---------------------------------------------------------------------------


@pytest.fixture
def make_event(sample_account_id: uuid.UUID, sample_folder_id: uuid.UUID):
    """Factory for creating SyncEvents of various types."""

    def _factory(
        event_type: str = "mail_received",
        uid: int = 100,
        **kwargs: Any,
    ) -> SyncEvent:
        base = {
            "account_id": sample_account_id,
            "folder_id": sample_folder_id,
            "timestamp": datetime.now(timezone.utc),
        }
        base.update(kwargs)

        if event_type == "mail_received":
            return MailReceived(uid=uid, **base)
        elif event_type == "mail_deleted":
            return MailDeleted(uid=uid, **base)
        elif event_type == "mail_moved":
            return MailMoved(uid=uid, **base)
        elif event_type == "mail_trashed":
            return MailTrashed(uid=uid, **base)
        elif event_type == "mail_spam_detected":
            return MailSpamDetected(uid=uid, **base)
        elif event_type == "flags_changed":
            return FlagsChanged(uid=uid, **base)
        else:
            raise ValueError(f"Unknown event type: {event_type}")

    return _factory


# ---------------------------------------------------------------------------
# Mock IMAP connector
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_imap_connector() -> MagicMock:
    """Mock IMAPConnector with controllable capabilities."""
    connector = MagicMock()
    connector.account_name = "test-account"
    connector.capabilities = {"IMAP4rev1", "IDLE", "CONDSTORE"}
    connector.has_condstore = MagicMock(return_value=True)
    connector.has_qresync = MagicMock(return_value=False)
    connector.has_special_use = MagicMock(return_value=True)
    connector.has_idle = MagicMock(return_value=True)
    connector.connect = AsyncMock()
    connector.connect_with_retry = AsyncMock()
    connector.close = AsyncMock()
    return connector


@pytest.fixture
def mock_imap_extended() -> MagicMock:
    """Mock AsyncIMAPExtended for IMAP operations."""
    extended = MagicMock()
    extended.capabilities = {"IMAP4rev1", "IDLE", "CONDSTORE"}
    extended.has_capability = MagicMock(side_effect=lambda c: c in extended.capabilities)

    # SELECT responses
    select_result = MagicMock()
    select_result.ok = True
    select_result.uidvalidity = 1234
    select_result.uidnext = 100
    select_result.highestmodseq = 5000
    select_result.exists = 50
    select_result.raw_lines = []

    extended.select_plain = AsyncMock(return_value=select_result)
    extended.select_condstore = AsyncMock(return_value=select_result)
    extended.select_qresync = AsyncMock(return_value=select_result)

    # LIST responses
    extended.list_folders = AsyncMock(return_value=[])
    extended.list_special_use = AsyncMock(return_value=[])

    # Underlying client mock
    extended.client = MagicMock()
    extended.client.get_state = MagicMock(return_value="SELECTED")
    extended.client.uid = AsyncMock()
    extended.client.login = AsyncMock()
    extended.client.logout = AsyncMock()
    extended.client.move = AsyncMock()

    return extended


# ---------------------------------------------------------------------------
# Mock semantic store
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_semantic_store() -> AsyncMock:
    """Mock SemanticStore for spam pipeline tests."""
    store = AsyncMock()
    store.ensure_collection = AsyncMock(return_value=True)
    store.upsert = AsyncMock(return_value=True)
    store.search = AsyncMock(return_value=[])
    store.embed = AsyncMock(return_value=[[0.1] * 1536])
    store.update_payload = AsyncMock(return_value=True)
    store._qdrant = AsyncMock()
    store._collection = "test_embeddings"
    return store


# ---------------------------------------------------------------------------
# Mock action propagator
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_action_propagator() -> AsyncMock:
    """Mock ActionPropagator for rules executor tests."""
    propagator = AsyncMock()
    propagator.execute_imap = AsyncMock(return_value=True)
    propagator.execute_forward = AsyncMock(return_value=True)
    propagator.move_to_spam = AsyncMock(return_value=True)
    return propagator
