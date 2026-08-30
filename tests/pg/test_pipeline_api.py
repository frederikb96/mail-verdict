"""
The pipeline configuration API against a real Postgres.

pipeline_revisions is process-wide state shared with every other pg test
in this session (the container is session-scoped) -- every mutating test
here establishes its own known baseline with an unconditional PUT before
asserting anything, rather than assuming what a prior test left behind.
"""

from __future__ import annotations

import functools
import itertools
import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from mail_verdict.api.pipeline import router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.settings.credentials import ProviderCredentialRepository
from mail_verdict.settings.service import SettingsService

_imap_uid_counter = itertools.count(1)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A single persistent TestClient portal for the whole test -- a new
    `with TestClient(...)` per call would each open its own event loop in
    its own thread, and the shared migrated_db's asyncpg connections
    would then bounce between them and fail with 'attached to a
    different loop' the moment a second call touches the database."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _put(client: TestClient, migrated_db: DatabaseConnection, document: dict) -> dict:
    """PUT a document unconditionally (no base_revision) and return the response body."""
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.put("/pipeline", json=document)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _seed_account_and_folder(migrated_db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
    account_id, folder_id = uuid.uuid4(), uuid.uuid4()
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO accounts "
                "(id, name, imap_host, imap_port, imap_user, imap_password) "
                "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
                "'\\x00' || convert_to('pw', 'UTF8'))"
            ),
            {"id": account_id, "name": f"acct-{account_id}"},
        )
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name) "
                "VALUES (:id, :account_id, 'INBOX')"
            ),
            {"id": folder_id, "account_id": account_id},
        )
        await session.execute(
            text(
                "INSERT INTO account_prefs (account_id, spam_enabled) "
                "VALUES (:account_id, true)"
            ),
            {"account_id": account_id},
        )
        await session.commit()
    return account_id, folder_id


async def _seed_message(
    migrated_db: DatabaseConnection, *, account_id: uuid.UUID, folder_id: uuid.UUID,
) -> uuid.UUID:
    mail_id = uuid.uuid4()
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
                "subject, body_text, received_at, size_bytes) "
                "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :message_id, "
                "'sender@example.com', 'Cheap viagra offer', 'Buy now.', now(), 1024)"
            ),
            {
                "id": mail_id, "account_id": account_id, "folder_id": folder_id,
                "imap_uid": next(_imap_uid_counter), "thread_id": uuid.uuid4(),
                "message_id": f"<{uuid.uuid4()}@example.com>",
            },
        )
        await session.commit()
    return mail_id


async def _configure_fake_ai_provider(migrated_db: DatabaseConnection) -> SettingsService:
    """A SettingsService with ai.provider='fake', persisted -- so the
    running-on-the-portal-loop classify stage's own settings read (made
    through this same service instance) sees it."""
    settings_service = SettingsService(migrated_db)
    await settings_service.load()
    await settings_service.update("ai", {"provider": "fake"})
    return settings_service


async def _seed_junk_folder(migrated_db: DatabaseConnection, account_id: uuid.UUID) -> None:
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use) "
                "VALUES (:id, :account_id, 'Junk', 'junk')"
            ),
            {"id": uuid.uuid4(), "account_id": account_id},
        )
        await session.commit()


async def _count_verdicts_for_mail(migrated_db: DatabaseConnection, mail_id: uuid.UUID) -> int:
    async with migrated_db.session() as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM verdicts WHERE mail_id = :m"), {"m": mail_id},
            )
        ).scalar_one()


def test_get_pipeline_returns_a_document(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.get("/pipeline")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["revision"], int)
    assert isinstance(body["stages"], list)
    assert isinstance(body["warnings"], list)


def test_put_rejects_unknown_stage_type(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.put(
            "/pipeline",
            json={"enabled": True, "stages": [
                {"stage_id": "s1", "type": "not-a-real-type", "config": {}},
            ]},
        )
    assert resp.status_code == 400
    assert "unknown stage type" in resp.json()["detail"][0]


def test_put_rejects_unknown_effect(client: TestClient, migrated_db: DatabaseConnection) -> None:
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.put(
            "/pipeline",
            json={"enabled": True, "stages": [
                {
                    "stage_id": "s1", "type": "match",
                    "config": {"when": {}, "effects": [{"teleport": {}}]},
                },
            ]},
        )
    assert resp.status_code == 400


def test_put_rejects_unknown_condition_type(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.put(
            "/pipeline",
            json={"enabled": True, "stages": [
                {
                    "stage_id": "s1", "type": "match",
                    "config": {"when": {"telepathy_detects": "spam"}, "effects": []},
                },
            ]},
        )
    assert resp.status_code == 400
    assert "unknown condition type" in resp.json()["detail"][0]


def test_put_rejects_duplicate_stage_ids(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.put(
            "/pipeline",
            json={"enabled": True, "stages": [
                {"stage_id": "dup", "type": "match", "config": {"when": {}, "effects": []}},
                {"stage_id": "dup", "type": "match", "config": {"when": {}, "effects": []}},
            ]},
        )
    assert resp.status_code == 400
    assert "duplicate" in resp.json()["detail"][0]


def test_put_accepts_an_unresolved_folder_with_a_warning(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    """A folder_name that does not exist yet is accepted -- reported as a
    warning, never a rejection."""
    account_id, _ = client.portal.call(_seed_account_and_folder, migrated_db)
    document = {
        "enabled": True, "stages": [
            {
                "stage_id": "move-to-nowhere", "type": "match", "accounts": [str(account_id)],
                "config": {
                    "when": {"subject_contains": "invoice"},
                    "effects": [{"move": {"folder_name": "DoesNotExist-" + uuid.uuid4().hex}}],
                },
            },
        ],
    }
    body = _put(client, migrated_db, document)
    assert body["revision"] > 0
    assert any(w["stage_id"] == "move-to-nowhere" for w in body["warnings"])


def test_put_is_optimistic_about_base_revision(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    baseline = _put(client, migrated_db, {"enabled": True, "stages": []})
    stale_revision = baseline["revision"]

    # A concurrent writer advances the revision.
    _put(client, migrated_db, {"enabled": True, "stages": []})

    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.put(
            "/pipeline",
            json={"base_revision": stale_revision, "enabled": True, "stages": []},
        )
    assert resp.status_code == 409


def test_create_update_delete_a_stage(client: TestClient, migrated_db: DatabaseConnection) -> None:
    stage_id = f"s-{uuid.uuid4().hex[:8]}"
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        create_resp = client.post(
            "/pipeline/stages",
            json={
                "stage_id": stage_id, "type": "match", "name": "Test stage",
                "config": {"when": {"subject_contains": "test"}, "effects": []},
            },
        )
        assert create_resp.status_code == 200, create_resp.text
        created = next(s for s in create_resp.json()["stages"] if s["stage_id"] == stage_id)
        assert created["name"] == "Test stage"

        # Duplicate stage_id is rejected.
        dup_resp = client.post(
            "/pipeline/stages",
            json={"stage_id": stage_id, "type": "match", "config": {}},
        )
        assert dup_resp.status_code == 400

        patch_resp = client.patch(f"/pipeline/stages/{stage_id}", json={"enabled": False})
        assert patch_resp.status_code == 200
        patched = next(s for s in patch_resp.json()["stages"] if s["stage_id"] == stage_id)
        assert patched["enabled"] is False
        assert patched["name"] == "Test stage"  # untouched fields survive a partial update

        del_resp = client.delete(f"/pipeline/stages/{stage_id}")
        assert del_resp.status_code == 200
        assert all(s["stage_id"] != stage_id for s in del_resp.json()["stages"])

        missing_resp = client.delete(f"/pipeline/stages/{stage_id}")
        assert missing_resp.status_code == 404


def test_reorder_requires_a_full_permutation(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    document = {
        "enabled": True, "stages": [
            {"stage_id": "a", "type": "match", "config": {"when": {}, "effects": []}},
            {"stage_id": "b", "type": "match", "config": {"when": {}, "effects": []}},
        ],
    }
    _put(client, migrated_db, document)

    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        partial_resp = client.post("/pipeline/stages/reorder", json={"order": ["a"]})
        assert partial_resp.status_code == 400

        ok_resp = client.post("/pipeline/stages/reorder", json={"order": ["b", "a"]})
        assert ok_resp.status_code == 200
        ids = [s["stage_id"] for s in ok_resp.json()["stages"]]
        assert ids == ["b", "a"]


def test_stage_types_lists_match_and_classify_with_schemas(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.get("/pipeline/stage-types")
    assert resp.status_code == 200
    by_type = {t["type"]: t for t in resp.json()}
    assert set(by_type) >= {"match", "classify"}
    assert by_type["classify"]["runs_on"] == ["live"]
    assert "properties" in by_type["match"]["schema"]


def test_revisions_history_and_restore(client: TestClient, migrated_db: DatabaseConnection) -> None:
    marker_stage_id = f"restore-marker-{uuid.uuid4().hex[:8]}"
    first = _put(client, migrated_db, {
        "enabled": True, "stages": [
            {"stage_id": marker_stage_id, "type": "match", "config": {"when": {}, "effects": []}},
        ],
    })
    first_revision = first["revision"]
    _put(client, migrated_db, {"enabled": True, "stages": []})

    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        history_resp = client.get("/pipeline/revisions")
        assert history_resp.status_code == 200
        revisions = [r["revision"] for r in history_resp.json()]
        assert first_revision in revisions

        restore_resp = client.post(f"/pipeline/revisions/{first_revision}/restore")
        assert restore_resp.status_code == 200
        restored = restore_resp.json()
        assert restored["revision"] > first_revision
        assert any(s["stage_id"] == marker_stage_id for s in restored["stages"])

        missing_resp = client.post("/pipeline/revisions/999999999/restore")
        assert missing_resp.status_code == 404


def test_health_reports_an_unresolved_folder(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    account_id, _ = client.portal.call(_seed_account_and_folder, migrated_db)
    missing_name = "Missing-" + uuid.uuid4().hex
    _put(client, migrated_db, {
        "enabled": True, "stages": [
            {
                "stage_id": "health-check-stage", "type": "match", "accounts": [str(account_id)],
                "config": {"when": {}, "effects": [{"move": {"folder_name": missing_name}}]},
            },
        ],
    })
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.get("/pipeline/health")
    assert resp.status_code == 200
    entries = resp.json()
    assert any(
        e["stage_id"] == "health-check-stage" and not e["ok"]
        and missing_name in (e["detail"] or "")
        for e in entries
    )


def test_dry_run_classifies_without_writing_anything(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    # Every direct DB touch below runs via client.portal.call(), the same
    # event loop the TestClient itself dispatches requests on -- see the
    # client fixture's docstring for why mixing loops on one asyncpg pool
    # breaks.
    account_id, folder_id = client.portal.call(_seed_account_and_folder, migrated_db)
    client.portal.call(_seed_junk_folder, migrated_db, account_id)
    mail_id = client.portal.call(
        functools.partial(_seed_message, account_id=account_id, folder_id=folder_id),
        migrated_db,
    )
    settings_service = client.portal.call(_configure_fake_ai_provider, migrated_db)

    _put(client, migrated_db, {
        "enabled": True, "stages": [
            {"stage_id": "classify", "type": "classify", "config": {}},
            {
                "stage_id": "move-spam", "type": "match",
                "config": {
                    "when": {"verdict_is": "spam"},
                    "effects": [{"move": {"special_use": "junk"}}],
                },
            },
        ],
    })

    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db), \
         patch("mail_verdict.api.pipeline.get_settings_service", return_value=settings_service), \
         patch(
             "mail_verdict.api.pipeline.get_provider_credential_repo",
             return_value=ProviderCredentialRepository(migrated_db, ""),
         ), \
         patch("mail_verdict.api.pipeline.get_event_ring", return_value=None):
        resp = client.post("/pipeline/test", json={"message_id": str(mail_id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    classify_entry = next(e for e in body["trace"] if e["stage_id"] == "classify")
    assert classify_entry["matched"] is True
    move_entry = next(e for e in body["trace"] if e["stage_id"] == "move-spam")
    assert move_entry["matched"] is True
    assert "would move to" in move_entry["applied"][0]["detail"]  # dry run: resolved, never written

    assert client.portal.call(_count_verdicts_for_mail, migrated_db, mail_id) == 0


def test_dry_run_missing_message_is_404(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db):
        resp = client.post("/pipeline/test", json={"message_id": str(uuid.uuid4())})
    assert resp.status_code == 404


def test_dry_run_reports_a_misconfigured_stage_instead_of_crashing(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    """A move to a folder that does not resolve raises StageMisconfigured
    inside the runner -- the endpoint must report it in the response
    rather than surface it as an unhandled 500."""
    account_id, folder_id = client.portal.call(_seed_account_and_folder, migrated_db)
    mail_id = client.portal.call(
        functools.partial(_seed_message, account_id=account_id, folder_id=folder_id),
        migrated_db,
    )
    _put(client, migrated_db, {
        "enabled": True, "stages": [
            {
                "stage_id": "move-nowhere", "type": "match", "accounts": [str(account_id)],
                "config": {"when": {}, "effects": [{"move": {"special_use": "junk"}}]},
            },
        ],
    })
    settings_service = client.portal.call(_configure_fake_ai_provider, migrated_db)
    with patch("mail_verdict.api.pipeline.get_db_connection", return_value=migrated_db), \
         patch("mail_verdict.api.pipeline.get_settings_service", return_value=settings_service), \
         patch(
             "mail_verdict.api.pipeline.get_provider_credential_repo",
             return_value=ProviderCredentialRepository(migrated_db, ""),
         ), \
         patch("mail_verdict.api.pipeline.get_event_ring", return_value=None):
        resp = client.post("/pipeline/test", json={"message_id": str(mail_id)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert "does not resolve" in body["skip_reason"]
