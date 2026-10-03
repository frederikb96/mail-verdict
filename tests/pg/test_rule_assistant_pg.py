"""
The rule assistant against a real Postgres, with the `fake` provider
standing in for the model: POST /pipeline/assistant end to end, then the
ordinary pipeline write that Accept implies. Every assertion is scoped to
the ids this test seeded -- pipeline_revisions and the account list are
shared by the whole session.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from mail_verdict.api.pipeline import router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.rules import assistant
from mail_verdict.settings.credentials import ProviderCredentialRepository
from mail_verdict.settings.service import SettingsService
from tests.pg.test_pipeline_api import (
    _configure_fake_ai_provider,
    _put,
    _seed_account_and_folder,
    _seed_message,
)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """One persistent portal for the whole test, so every database touch
    shares the TestClient's event loop (see test_pipeline_api.py)."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _patched(migrated_db: DatabaseConnection, settings_service: SettingsService) -> ExitStack:
    stack = ExitStack()
    for name, value in (
        ("get_db_connection", migrated_db),
        ("get_settings_service", settings_service),
        ("get_provider_credential_repo", ProviderCredentialRepository(migrated_db, "")),
        ("get_event_ring", None),
    ):
        stack.enter_context(patch(f"mail_verdict.api.pipeline.{name}", return_value=value))
    return stack


def _baseline(client: TestClient, migrated_db: DatabaseConnection) -> dict:
    """A known pipeline: one halting rule that already catches the seeded sender."""
    return _put(client, migrated_db, {
        "enabled": True, "stages": [
            {
                "stage_id": "earlier", "type": "match", "name": "Earlier", "halt": True,
                "config": {
                    "when": {"sender_match": "sender@example.com"},
                    "effects": [{"set_flags": {"seen": True}}],
                },
            },
        ],
    })


def test_proposal_then_accept_is_an_ordinary_pipeline_write(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    account_id, folder_id = client.portal.call(_seed_account_and_folder, migrated_db)
    mail_id = client.portal.call(
        functools.partial(_seed_message, account_id=account_id, folder_id=folder_id),
        migrated_db,
    )
    settings_service = client.portal.call(_configure_fake_ai_provider, migrated_db)
    base = _baseline(client, migrated_db)

    with _patched(migrated_db, settings_service):
        resp = client.post(
            "/pipeline/assistant",
            json={"message_id": str(mail_id), "prompt": "  flag these  "},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        change = body["change"]
        assert change["kind"] == "new_rule" and change["is_new"] is True
        assert change["base_revision"] == base["revision"]
        stage = change["stage"]
        assert stage["stage_id"].startswith(f"assistant-{str(mail_id)[:8]}")
        assert stage["type"] == "match" and stage["enabled"] is True
        assert stage["accounts"] == [str(account_id)]
        assert change["before_text"] is None and stage["stage_id"] in change["after_text"]
        assert body["model"] == "fake" and body["model_calls"] == 0

        # Scoped to the account this test seeded: one mail, which the rule catches.
        preview = body["preview"]
        assert (preview["sample_size"], preview["matched_before"], preview["matched_after"]) == (
            1, 0, 1,
        )
        assert preview["examples"] == [
            {"from_addr": "sender@example.com", "subject": "Cheap viagra offer"},
        ]
        assert len(body["warnings"]) == 1 and "Earlier" in body["warnings"][0]

        # Nothing was stored by asking.
        assert client.get("/pipeline").json()["revision"] == base["revision"]

        # Accept: the existing create-stage endpoint with the proposal's base_revision.
        accept = {**stage, "base_revision": change["base_revision"]}
        written = client.post("/pipeline/stages", json=accept)
        assert written.status_code == 200, written.text
        stage_ids = [s["stage_id"] for s in written.json()["stages"]]
        assert stage_ids == ["earlier", stage["stage_id"]]

        # The same proposal accepted twice finds the rules changed meanwhile.
        assert client.post("/pipeline/stages", json=accept).status_code == 409

        # Asking again now proposes a different, still-free id.
        again = client.post(
            "/pipeline/assistant", json={"message_id": str(mail_id), "prompt": "again"},
        ).json()
        assert again["change"]["stage"]["stage_id"] not in stage_ids


def test_unknown_message_is_404_and_an_empty_prompt_is_refused(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    settings_service = client.portal.call(_configure_fake_ai_provider, migrated_db)
    with _patched(migrated_db, settings_service):
        missing = client.post(
            "/pipeline/assistant", json={"message_id": str(uuid.uuid4()), "prompt": "x"},
        )
        assert missing.status_code == 404
        for prompt in ("", "   "):
            refused = client.post(
                "/pipeline/assistant", json={"message_id": str(uuid.uuid4()), "prompt": prompt},
            )
            assert refused.status_code == 422


def test_search_matches_substrings_literally_and_only_in_its_account(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    """`%` and `_` in a search term are literal, and another account's
    mail never shows up."""
    account_id, folder_id = client.portal.call(_seed_account_and_folder, migrated_db)
    other_account, other_folder = client.portal.call(_seed_account_and_folder, migrated_db)

    async def seed() -> None:
        async with migrated_db.session() as session:
            for account, folder, subject in (
                (account_id, folder_id, "50% off_deal"),
                (account_id, folder_id, "50X offXdeal"),
                (other_account, other_folder, "50% off_deal"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO messages (id, account_id, folder_id, imap_uid, thread_id, "
                        "message_id, from_addr, subject, received_at, size_bytes) VALUES "
                        "(:id, :a, :f, :uid, :t, :mid, 'shop@example.com', :s, now(), 1)"
                    ),
                    {
                        "id": uuid.uuid4(), "a": account, "f": folder,
                        "uid": uuid.uuid4().int % 10**9, "t": uuid.uuid4(),
                        "mid": f"<{uuid.uuid4()}@example.com>", "s": subject,
                    },
                )
            await session.commit()

    async def count(from_contains: str, subject_contains: str) -> tuple[int, int]:
        async with migrated_db.session() as session:
            total = (await session.execute(
                assistant.search_count_statement(account_id, from_contains, subject_contains),
            )).scalar_one()
            sample = (await session.execute(
                assistant.search_sample_statement(account_id, from_contains, subject_contains),
            )).all()
        return total, len(sample)

    client.portal.call(seed)
    assert client.portal.call(count, "", "off_deal") == (1, 1)
    assert client.portal.call(count, "", "50%") == (1, 1)
    assert client.portal.call(count, "SHOP@", "50") == (2, 2)
