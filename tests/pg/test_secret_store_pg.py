"""
The secret store against a real database: names listed, values never
returned by any endpoint, ciphertext at rest, replacement and deletion, and
refusal when no encryption key is configured.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from mail_verdict.api.secrets import router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.settings.secret_store import (
    SecretRepository,
    SecretUnavailableError,
    init_secret_repo,
    reset_secret_repo,
)

_KEY = "0123456789abcdef" * 4
_VALUE = "tok-ZXCVBNM-not-a-real-token"


@pytest.fixture()
def client(migrated_db: DatabaseConnection) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        c.portal.call(init_secret_repo, migrated_db, _KEY)
        try:
            yield c
        finally:
            reset_secret_repo()


def test_round_trip_never_returns_the_value(client: TestClient) -> None:
    put = client.put("/secrets/ROUND_TRIP", json={"value": _VALUE})
    assert put.status_code == 200
    assert put.json() == {"name": "ROUND_TRIP", "created": True}

    listing = client.get("/secrets")
    assert listing.status_code == 200
    mine = [s for s in listing.json() if s["name"] == "ROUND_TRIP"]
    assert len(mine) == 1
    assert set(mine[0]) == {"name", "created_at", "updated_at"}

    replaced = client.put("/secrets/ROUND_TRIP", json={"value": _VALUE + "-2"})
    assert replaced.json()["created"] is False

    for body in (put.text, listing.text, replaced.text):
        assert _VALUE not in body

    assert client.delete("/secrets/ROUND_TRIP").status_code == 204
    assert client.delete("/secrets/ROUND_TRIP").status_code == 404
    assert all(s["name"] != "ROUND_TRIP" for s in client.get("/secrets").json())


def test_invalid_names_and_values_are_rejected_without_echo(client: TestClient) -> None:
    assert client.put("/secrets/1bad", json={"value": _VALUE}).status_code == 400
    empty = client.put("/secrets/GOOD_NAME", json={"value": ""})
    assert empty.status_code == 400
    too_long = client.put("/secrets/GOOD_NAME", json={"value": "x" * 9000})
    assert too_long.status_code == 400
    assert "x" * 100 not in too_long.text


def test_value_is_ciphertext_at_rest_and_resolves_back(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    client.put("/secrets/AT_REST", json={"value": _VALUE})

    async def stored() -> bytes:
        async with migrated_db.session() as session:
            return bytes(
                (
                    await session.execute(
                        text("SELECT encrypted_value FROM secrets WHERE name = 'AT_REST'")
                    )
                ).scalar_one()
            )

    assert _VALUE.encode() not in client.portal.call(stored)

    repo = SecretRepository(migrated_db, _KEY)
    assert client.portal.call(repo.resolve_many, ["AT_REST"]) == {"AT_REST": _VALUE}
    client.delete("/secrets/AT_REST")


def test_resolving_a_missing_secret_names_it_and_nothing_else(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    repo = SecretRepository(migrated_db, _KEY)
    with pytest.raises(SecretUnavailableError, match="NOT_THERE"):
        client.portal.call(repo.resolve_many, ["NOT_THERE"])


def test_a_wrong_key_does_not_decrypt(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    client.put("/secrets/WRONG_KEY", json={"value": _VALUE})
    other = SecretRepository(migrated_db, "fedcba9876543210" * 4)
    with pytest.raises(SecretUnavailableError) as exc_info:
        client.portal.call(other.resolve_many, ["WRONG_KEY"])
    assert _VALUE not in str(exc_info.value)
    client.delete("/secrets/WRONG_KEY")


def test_storing_without_an_encryption_key_is_a_clear_400(
    migrated_db: DatabaseConnection,
) -> None:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        c.portal.call(init_secret_repo, migrated_db, "")
        try:
            resp = c.put("/secrets/NO_KEY", json={"value": _VALUE})
            assert resp.status_code == 400
            assert "ENCRYPTION_KEY" in resp.json()["detail"]
            assert _VALUE not in resp.text
        finally:
            reset_secret_repo()
