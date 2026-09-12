"""
has_attachments and verdict_is_spam on list rows, through the account list
(flat and threaded) and a unified view's list -- computed in the list
query, read only.

The verdict fixtures are the case where the list has to choose: two
messages each carry an older and a newer verdict, one flipped each way, so
a list that took the first row, or the spam-most one, disagrees with the
reading pane on at least one of them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from mail_verdict.api.mails import account_router as messages_router
from mail_verdict.api.unified import unified_router
from mail_verdict.database.connection import DatabaseConnection
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders, _seed_messages


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """One persistent portal for the whole test -- see test_identities_api_pg.py."""
    app = FastAPI()
    app.include_router(messages_router)
    app.include_router(unified_router)
    with TestClient(app) as c:
        yield c


async def _verdict(session: Any, mail_id: uuid.UUID, account_id: uuid.UUID, *, is_spam: bool,
                   source: str, age: timedelta) -> None:
    await session.execute(
        text(
            "INSERT INTO verdicts (id, mail_id, account_id, msg_key, is_spam, source, created_at) "
            "VALUES (:id, :mail_id, :account_id, :msg_key, :is_spam, :source, "
            ":created_at)"
        ),
        {
            "id": uuid.uuid4(), "mail_id": mail_id, "account_id": account_id,
            "msg_key": f"k-{uuid.uuid4()}", "is_spam": is_spam, "source": source,
            "created_at": datetime.now(timezone.utc) - age,
        },
    )


async def _seed(db: DatabaseConnection) -> dict[str, Any]:
    async with db.session() as session:
        account_id, inbox_id, _junk = await _seed_account_two_folders(session)
        attached, now_ham, now_spam, plain = await _seed_messages(session, account_id, inbox_id, 4)
        await session.execute(
            text(
                "INSERT INTO attachments (id, message_id, filename, content_type, data) "
                "VALUES (:id, :message_id, 'report.pdf', 'application/pdf', :data)"
            ),
            {"id": uuid.uuid4(), "message_id": attached, "data": b"%PDF"},
        )
        await _verdict(session, now_ham, account_id, is_spam=True, source="ai",
                       age=timedelta(hours=1))
        await _verdict(session, now_ham, account_id, is_spam=False, source="user_feedback",
                       age=timedelta(minutes=1))
        await _verdict(session, now_spam, account_id, is_spam=False, source="ai",
                       age=timedelta(hours=1))
        await _verdict(session, now_spam, account_id, is_spam=True, source="user_feedback",
                       age=timedelta(minutes=1))
        view_id, view_name = uuid.uuid4(), f"marks-{uuid.uuid4().hex[:8]}"
        await session.execute(
            text("INSERT INTO unified_views (id, name, position) VALUES (:id, :name, 0)"),
            {"id": view_id, "name": view_name},
        )
        await session.execute(
            text("INSERT INTO unified_view_folders (view_id, folder_id) VALUES (:v, :f)"),
            {"v": view_id, "f": inbox_id},
        )
        await session.commit()
    return {
        "account_id": account_id, "inbox_id": inbox_id, "view_name": view_name,
        "expected": {
            str(attached): (True, None),
            str(now_ham): (False, False),
            str(now_spam): (False, True),
            str(plain): (False, None),
        },
    }


def _marks(rows: list[dict[str, Any]]) -> dict[str, tuple[bool, bool | None]]:
    return {r["id"]: (r["has_attachments"], r["verdict_is_spam"]) for r in rows}


@pytest.mark.parametrize("threaded", [False, True])
def test_the_account_list_carries_each_rows_attachment_and_latest_verdict(
    client: TestClient, migrated_db: DatabaseConnection, threaded: bool,
) -> None:
    seeded = client.portal.call(_seed, migrated_db)
    response = client.get(
        f"/accounts/{seeded['account_id']}/messages",
        params={"folder_id": str(seeded["inbox_id"]), "threaded": threaded},
    )
    assert response.status_code == 200, response.text
    assert _marks(response.json()["messages"]) == seeded["expected"]


def test_a_unified_views_list_carries_them_too(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    seeded = client.portal.call(_seed, migrated_db)
    response = client.get("/unified/mails", params={"folder_name": seeded["view_name"]})
    assert response.status_code == 200, response.text
    assert _marks(response.json()["messages"]) == seeded["expected"]


def test_both_fields_are_part_of_the_published_schema() -> None:
    from mail_verdict.server import build_api_app

    schema = build_api_app().openapi()["components"]["schemas"]["MessageSummary"]
    assert {"has_attachments", "verdict_is_spam"} <= set(schema["required"])
