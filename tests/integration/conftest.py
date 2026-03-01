"""
Integration test conftest: real DB, real Qdrant, Stalwart helpers.

These fixtures connect to the Podman Compose test services.
"""

from __future__ import annotations

import os
import uuid

import pytest

from mail_verdict.config import DatabaseConfig, QdrantConfig
from tests.helpers.stalwart import StalwartClient

# Service URLs for podman-compose.test.yaml
TEST_POSTGRES_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql+asyncpg://mail_verdict_test:mail_verdict_test@localhost:5433/mail_verdict_test",
)
TEST_QDRANT_HOST = os.environ.get("TEST_QDRANT_HOST", "localhost")
TEST_QDRANT_PORT = int(os.environ.get("TEST_QDRANT_PORT", "6334"))
TEST_STALWART_URL = os.environ.get("TEST_STALWART_URL", "http://localhost:8880")


@pytest.fixture(scope="session")
def integration_db_config() -> DatabaseConfig:
    """Database config pointing to the test Postgres instance."""
    return DatabaseConfig(
        url=TEST_POSTGRES_URL,
        pool_size=3,
        max_overflow=1,
    )


@pytest.fixture(scope="session")
def integration_qdrant_config() -> QdrantConfig:
    """Qdrant config pointing to the test Qdrant instance."""
    return QdrantConfig(
        host=TEST_QDRANT_HOST,
        port=TEST_QDRANT_PORT,
        collection_name=f"test_{uuid.uuid4().hex[:8]}",
    )


@pytest.fixture(scope="session")
def stalwart_client() -> StalwartClient:
    """Stalwart client for integration tests."""
    return StalwartClient(base_url=TEST_STALWART_URL)
