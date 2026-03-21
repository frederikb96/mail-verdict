"""
Unit test conftest: mock fixtures for all external dependencies.
"""

from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.config.loader import InfraConfig, get_config, reset_config
from mail_verdict.rules.bus import EventBus
from mail_verdict.settings.defaults import SETTING_DEFAULTS
from tests.helpers.config_factory import make_config

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "emails"


@pytest.fixture()
def test_config_dict() -> dict[str, Any]:
    """Raw config dict with test defaults."""
    return make_config()


@pytest.fixture()
def test_config(
    monkeypatch: pytest.MonkeyPatch, test_config_dict: dict[str, Any],
) -> InfraConfig:
    """Parsed InfraConfig with test defaults loaded via singleton."""
    import mail_verdict.config.loader as loader

    reset_config()
    monkeypatch.setattr(loader, "_CONFIG", test_config_dict)
    loader._config_instance = None
    return get_config()


@pytest.fixture()
def test_settings() -> dict[str, dict[str, Any]]:
    """Default settings dict for tests."""
    return {k: dict(v) for k, v in SETTING_DEFAULTS.items()}


@pytest.fixture()
def mock_db_session() -> AsyncMock:
    """AsyncMock of SQLAlchemy async session."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture()
def mock_mailbox() -> MagicMock:
    """Mock imap-tools MailBox with preset folder, idle, and fetch methods."""
    mailbox = MagicMock()

    # Folder manager
    folder_mgr = MagicMock()
    folder_mgr.set = MagicMock()
    folder_mgr.list = MagicMock(return_value=[])
    folder_mgr.status = MagicMock(return_value={
        "MESSAGES": 0, "UIDNEXT": 1, "UIDVALIDITY": 1, "UNSEEN": 0,
    })
    mailbox.folder = folder_mgr

    # Idle manager
    idle_mgr = MagicMock()
    idle_mgr.wait = MagicMock(return_value=[])
    idle_mgr.start = MagicMock()
    idle_mgr.stop = MagicMock()
    mailbox.idle = idle_mgr

    # Fetch, uids, flag, move, copy, delete
    mailbox.fetch = MagicMock(return_value=iter([]))
    mailbox.uids = MagicMock(return_value=[])
    mailbox.flag = MagicMock()
    mailbox.move = MagicMock()
    mailbox.copy = MagicMock()
    mailbox.delete = MagicMock()
    mailbox.login = MagicMock(return_value=mailbox)
    mailbox.logout = MagicMock()

    # Underlying imaplib client for NOOP health check
    client = MagicMock()
    client.noop = MagicMock(return_value=("OK", []))
    mailbox.client = client

    return mailbox


@pytest.fixture()
def mock_qdrant() -> MagicMock:
    """Mock AsyncQdrantClient with configurable search results."""
    client = MagicMock()

    collections_response = MagicMock()
    collections_response.collections = []
    client.get_collections = AsyncMock(return_value=collections_response)
    client.create_collection = AsyncMock()
    client.create_payload_index = AsyncMock()
    client.retrieve = AsyncMock(return_value=[])
    client.upsert = AsyncMock()
    client.set_payload = AsyncMock()

    query_response = MagicMock()
    query_response.points = []
    client.query_points = AsyncMock(return_value=query_response)

    return client


@pytest.fixture()
def mock_openai() -> MagicMock:
    """Mock AsyncOpenAI client with fake embeddings and chat responses."""
    client = MagicMock()

    embedding_item = MagicMock()
    embedding_item.embedding = [0.1] * 3072
    embedding_response = MagicMock()
    embedding_response.data = [embedding_item]
    client.embeddings = MagicMock()
    client.embeddings.create = AsyncMock(return_value=embedding_response)

    chat_message = MagicMock()
    chat_message.content = '{"verdict": "not-spam"}'
    chat_choice = MagicMock()
    chat_choice.message = chat_message
    chat_response = MagicMock()
    chat_response.choices = [chat_choice]
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=chat_response)

    return client


@pytest.fixture()
def mock_event_bus() -> EventBus:
    """Real EventBus instance that records emitted events."""
    return EventBus()


@pytest.fixture()
def sample_email_bytes() -> bytes:
    """Raw bytes of ham_simple.eml."""
    return (FIXTURES_DIR / "ham_simple.eml").read_bytes()


@pytest.fixture()
def sample_email(sample_email_bytes: bytes) -> EmailMessage:
    """Parsed EmailMessage from ham_simple.eml."""
    msg = email.message_from_bytes(sample_email_bytes, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    return msg


@pytest.fixture()
def sample_spam_bytes() -> bytes:
    """Raw bytes of spam_pharmacy.eml."""
    return (FIXTURES_DIR / "spam_pharmacy.eml").read_bytes()


@pytest.fixture()
def sample_spam_email(sample_spam_bytes: bytes) -> EmailMessage:
    """Parsed EmailMessage from spam_pharmacy.eml."""
    msg = email.message_from_bytes(sample_spam_bytes, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    return msg
