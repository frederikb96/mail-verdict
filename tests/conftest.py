"""
Root conftest.py for MailVerdict tests.

Provides config isolation, temp directories, mock factories,
and shared email fixture loading.
"""

from __future__ import annotations

import email
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.config import loader as config_loader

FIXTURES_DIR = Path(__file__).parent / "fixtures"
EMAILS_DIR = FIXTURES_DIR / "emails"
CONFIG_DIR = Path(__file__).parent.parent / "config"


# ---------------------------------------------------------------------------
# Config isolation: each test gets a fresh config state
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Reset global config singleton before each test."""
    config_loader._config_instance = None
    config_loader._CONFIG = {}


@pytest.fixture
def config_path() -> Path:
    """Path to the project's default config.yaml."""
    return CONFIG_DIR / "config.yaml"


@pytest.fixture(autouse=True)
def set_config_env(config_path: Path, tmp_path: Path) -> None:
    """Point MAIL_VERDICT_CONFIG_PATH to the default config for test isolation."""
    os.environ["MAIL_VERDICT_CONFIG_PATH"] = str(config_path)
    # Prevent override file from affecting tests
    config_loader.OVERRIDE_CONFIG_PATH = tmp_path / "nonexistent.yaml"
    yield  # type: ignore[misc]
    os.environ.pop("MAIL_VERDICT_CONFIG_PATH", None)


# ---------------------------------------------------------------------------
# Temp directory fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_data_dir(tmp_path: Path) -> Path:
    """Isolated temp directory for test data."""
    data_dir = tmp_path / "test_data"
    data_dir.mkdir()
    return data_dir


# ---------------------------------------------------------------------------
# ID factory
# ---------------------------------------------------------------------------


@pytest.fixture
def make_uuid() -> type:
    """Factory that generates deterministic UUIDs for testing."""

    class UUIDFactory:
        """Generates reproducible UUIDs from integer seeds."""

        @staticmethod
        def make(seed: int = 0) -> uuid.UUID:
            """Generate a UUID from an integer seed."""
            return uuid.UUID(int=seed)

    return UUIDFactory


# ---------------------------------------------------------------------------
# Email fixture loading
# ---------------------------------------------------------------------------


@pytest.fixture
def load_eml():
    """
    Factory fixture that loads .eml files from the fixtures directory.

    Returns a callable that takes a filename (without path) and returns
    the parsed email.message.Message object.
    """

    def _load(name: str) -> email.message.Message:
        path = EMAILS_DIR / name
        if not path.exists():
            raise FileNotFoundError(f"Fixture email not found: {path}")
        return email.message_from_bytes(path.read_bytes())

    return _load


@pytest.fixture
def all_eml_files() -> list[Path]:
    """List all .eml fixture files."""
    return sorted(EMAILS_DIR.glob("*.eml"))


# ---------------------------------------------------------------------------
# Common timestamp factory
# ---------------------------------------------------------------------------


@pytest.fixture
def now_utc() -> datetime:
    """Current UTC timestamp for test assertions."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Mock factories for common services
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db_connection() -> MagicMock:
    """
    Mock DatabaseConnection with async session context manager.

    Returns a MagicMock with session() returning an AsyncMock session.
    """
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()

    mock_db = MagicMock()

    class _AsyncSessionCtx:
        async def __aenter__(self) -> AsyncMock:
            return mock_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_db.session = MagicMock(return_value=_AsyncSessionCtx())
    mock_db._mock_session = mock_session
    return mock_db


@pytest.fixture
def mock_qdrant_client() -> AsyncMock:
    """Mock Qdrant async client."""
    mock = AsyncMock()
    mock.get_collections = AsyncMock(return_value=MagicMock(collections=[]))
    mock.create_collection = AsyncMock()
    mock.create_payload_index = AsyncMock()
    mock.upsert = AsyncMock()
    mock.retrieve = AsyncMock(return_value=[])
    mock.set_payload = AsyncMock()
    mock.query_points = AsyncMock(return_value=MagicMock(points=[]))
    return mock


@pytest.fixture
def mock_openai_client() -> AsyncMock:
    """
    Mock OpenAI async client.

    Pre-configured to return a valid embedding and chat completion.
    """
    mock = AsyncMock()

    # Embeddings
    embedding_item = MagicMock()
    embedding_item.embedding = [0.1] * 1536
    embedding_response = MagicMock()
    embedding_response.data = [embedding_item]
    mock.embeddings = MagicMock()
    mock.embeddings.create = AsyncMock(return_value=embedding_response)

    # Chat completions
    choice = MagicMock()
    choice.message.content = '{"verdict": "not-spam"}'
    completion = MagicMock()
    completion.choices = [choice]
    mock.chat = MagicMock()
    mock.chat.completions = MagicMock()
    mock.chat.completions.create = AsyncMock(return_value=completion)

    return mock


@pytest.fixture
def sample_account_id() -> uuid.UUID:
    """Deterministic account UUID for tests."""
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def sample_folder_id() -> uuid.UUID:
    """Deterministic folder UUID for tests."""
    return uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def sample_mail_id() -> uuid.UUID:
    """Deterministic mail UUID for tests."""
    return uuid.UUID("00000000-0000-0000-0000-000000000003")
