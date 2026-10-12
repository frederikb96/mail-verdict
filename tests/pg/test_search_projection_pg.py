"""
Both search endpoints load their result rows through the narrow display
projection: the statements they run never name the raw source, the HTML
body or the headers of a message, and the semantic one never reads a body
at all. Proven on the SQL the database is actually sent.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from mail_verdict.api.search import search_messages
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.projection import DISPLAY_COLUMNS, display_statement
from mail_verdict.embeddings.search import semantic_search
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders
from tests.pg.test_message_embeddings import (
    _QUERY_VECTOR,
    _SPREAD,
    _seed_scored_pool,
    _spread_dates,
    _unique_model,
)
from tests.pg.test_search_api_pg import _seed_message

_NEVER = ("raw_source", "body_html", "raw_headers")


@contextmanager
def _captured_statements(db: DatabaseConnection) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    engine = db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_the_display_statement_names_no_body_or_raw_column() -> None:
    sql = str(display_statement([]).compile())

    for column in DISPLAY_COLUMNS:
        assert column in sql
    for forbidden in (*_NEVER, "body_text", "search_vector"):
        assert forbidden not in sql


@pytest.mark.asyncio
async def test_a_semantic_search_never_reads_a_body_or_a_raw_source(
    migrated_db: DatabaseConnection,
) -> None:
    model = _unique_model()
    account_id, _ids = await _seed_scored_pool(migrated_db, model, _SPREAD, _spread_dates())

    with _captured_statements(migrated_db) as statements:
        results = await semantic_search(
            migrated_db, query_vector=_QUERY_VECTOR, model=model, account_id=account_id,
            exact_scan_max_rows=0, k=10,
        )
        exact = await semantic_search(
            migrated_db, query_vector=_QUERY_VECTOR, model=model, account_id=account_id,
            exact_scan_max_rows=5000, k=10,
        )

    assert results and exact
    sent = "\n".join(statements)
    assert "FROM messages" in sent
    for forbidden in (*_NEVER, "body_text"):
        assert forbidden not in sent


@pytest.mark.asyncio
async def test_a_full_text_search_never_reads_a_raw_source_html_or_headers(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
        await _seed_message(
            session, account_id, inbox_id, uid=1, subject="zzprojection report",
            body_text="a long body mentioning zzprojection once",
            received_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=1),
        )
        await session.commit()

    with _captured_statements(migrated_db) as statements:
        page = await search_messages(
            q="zzprojection", account_id=account_id, folder_ids=None,
            fields=["subject", "body"], before=None, limit=50,
        )

    assert [r.subject for r in page.results] == ["zzprojection report"]
    assert page.results[0].snippet and "zzprojection" in page.results[0].snippet
    sent = "\n".join(statements)
    for forbidden in _NEVER:
        assert forbidden not in sent


@pytest.mark.asyncio
async def test_a_full_text_search_over_subject_alone_reads_no_body_for_its_page(
    migrated_db: DatabaseConnection,
) -> None:
    """The body is fetched for the snippet only when the body is searched."""
    async with migrated_db.session() as session:
        account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
        await _seed_message(session, account_id, inbox_id, uid=1, subject="zzsubjectonly")
        await session.commit()

    with _captured_statements(migrated_db) as statements:
        page = await search_messages(
            q="zzsubjectonly", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )

    assert len(page.results) == 1
    assert not [s for s in statements if "SELECT messages.id, messages.body_text" in s]


@pytest.mark.asyncio
async def test_the_projection_returns_what_a_result_row_shows(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
        await _seed_message(
            session, account_id, inbox_id, uid=7, subject="zzshown", from_addr="a@example.com",
        )
        await session.commit()
        row = (await session.execute(
            text("SELECT id FROM messages WHERE subject = 'zzshown' AND account_id = :a"),
            {"a": account_id},
        )).one()

    page = await search_messages(
        q="zzshown", account_id=account_id, folder_ids=None, fields=["subject"], before=None,
        limit=5,
    )

    (hit,) = page.results
    assert (hit.id, hit.from_addr, hit.is_glacier, hit.pending_sync) == (
        row.id, "a@example.com", False, False,
    )
