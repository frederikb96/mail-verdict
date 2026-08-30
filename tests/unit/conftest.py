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


def _anthropic_response(text: str) -> MagicMock:
    """Build a mock Anthropic Messages API response with one text block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


@pytest.fixture()
def anthropic_response() -> Any:
    """Factory building a mock Anthropic Messages API response for a given text."""
    return _anthropic_response


@pytest.fixture()
def mock_anthropic() -> MagicMock:
    """Mock AsyncAnthropic client returning a not-spam verdict by default."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_anthropic_response('{"verdict": "not-spam"}')
    )
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
