"""
Search, semantic search, unified views and the assistant (MCP) tools show a
user only their own accounts' mail.

Two users with one account each hold the same words, the same vectors and
views of the same name. Every surface asked as one of them must come back
with that user's rows alone, whatever account, folder or cursor the request
names.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastmcp import Client
from sqlalchemy import text

from mail_verdict.api.admin import AdminOwnerRequest, set_owner
from mail_verdict.api.mails import list_message_page
from mail_verdict.api.mcp_tools import mcp
from mail_verdict.api.schemas import UnifiedViewCreate, UnifiedViewUpdate
from mail_verdict.api.search import search_messages
from mail_verdict.api.unified import (
    create_unified_view,
    delete_unified_view,
    drop_account_memberships,
    list_unified_folders,
    list_unified_messages,
    member_folder_ids,
    set_folder_views,
    update_unified_view,
    view_ids_by_folder,
)
from mail_verdict.auth.principal import Principal
from mail_verdict.auth.scope import Scope
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import MessageEmbedding
from mail_verdict.database.repository import MessageRepository
from mail_verdict.embeddings.search import semantic_search
from tests.helpers.auth import as_principal, user_scope
from tests.pg.test_glacier_search_pg import (
    _enable_glacier,
    _seed_account,
    _seed_folder,
    _seed_glacier_message,
    _seed_live_message,
)
from tests.pg.test_message_embeddings import (
    _BOTH_PATHS,
    _QUERY_VECTOR,
    _SPREAD,
    _seed_scored_pool,
    _spread_dates,
    _unique_model,
)

_NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


class Side:
    """One user with one account, a live message and a glaciered one that all
    carry the same search word."""

    def __init__(
        self, user_id: uuid.UUID, account_id: uuid.UUID, folder_id: uuid.UUID,
        glacier_folder_id: uuid.UUID, live_id: uuid.UUID, glacier_id: uuid.UUID,
    ) -> None:
        self.user_id = user_id
        self.account_id = account_id
        self.folder_id = folder_id
        self.glacier_folder_id = glacier_folder_id
        self.live_id = live_id
        self.glacier_id = glacier_id

    @property
    def ids(self) -> set[uuid.UUID]:
        return {self.live_id, self.glacier_id}

    @property
    def scope(self) -> Scope:
        return user_scope(self.user_id, [self.account_id])


async def _seed_side(db: DatabaseConnection, word: str) -> Side:
    user_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, display_name, role) VALUES (:id, 'Isolation', 'user')"),
            {"id": user_id},
        )
        account_id = await _seed_account(session)
        await session.execute(
            text("UPDATE accounts SET is_active = true WHERE id = :id"), {"id": account_id},
        )
        await session.execute(
            text("INSERT INTO account_owners (account_id, user_id) VALUES (:a, :u)"),
            {"a": account_id, "u": user_id},
        )
        glacier_folder_id = await _enable_glacier(session, account_id)
        folder_id = await _seed_folder(session, account_id)
        live_id = await _seed_live_message(
            session, account_id=account_id, folder_id=folder_id,
            subject=f"Live {word}", received_at=_NOW,
        )
        glacier_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject=f"Glaciered {word}", received_at=_NOW - timedelta(days=1),
        )
    return Side(user_id, account_id, folder_id, glacier_folder_id, live_id, glacier_id)


@pytest_asyncio.fixture()
async def sides(migrated_db: DatabaseConnection) -> tuple[Side, Side, str]:
    word = f"zzisol{uuid.uuid4().hex[:10]}"
    return await _seed_side(migrated_db, word), await _seed_side(migrated_db, word), word


async def _search(side: Side, word: str, **kwargs: Any) -> set[uuid.UUID]:
    params: dict[str, Any] = {
        "q": word, "account_id": None, "folder_ids": None, "fields": None, "before": None,
        "limit": 50, "received_after": None, "received_before": None, "is_seen": None,
    }
    page = await search_messages(**{**params, **kwargs}, scope=side.scope)
    return {r.id for r in page.results}


class TestTextSearch:
    @pytest.mark.asyncio
    async def test_a_search_finds_only_the_callers_own_mail(
        self, migrated_db: DatabaseConnection, sides: tuple[Side, Side, str],
    ) -> None:
        a, b, word = sides

        assert await _search(a, word) == a.ids
        assert await _search(b, word) == b.ids

    @pytest.mark.asyncio
    async def test_naming_another_users_account_or_folder_finds_nothing(
        self, migrated_db: DatabaseConnection, sides: tuple[Side, Side, str],
    ) -> None:
        a, b, word = sides

        assert await _search(a, word, account_id=b.account_id) == set()
        assert await _search(a, word, folder_ids=[b.folder_id]) == set()
        # A glacier folder is named by id too, and is the sharper edge: the
        # glacier arm is reached through the folder, not the account.
        assert await _search(a, word, folder_ids=[b.glacier_folder_id]) == set()
        assert await _search(a, word, folder_ids=[a.glacier_folder_id]) == {a.glacier_id}

    @pytest.mark.asyncio
    async def test_a_cursor_naming_another_users_message_is_invalid(
        self, migrated_db: DatabaseConnection, sides: tuple[Side, Side, str],
    ) -> None:
        a, b, word = sides

        for foreign in (b.live_id, b.glacier_id):
            with pytest.raises(HTTPException) as refused:
                await _search(a, word, before=foreign)
            assert refused.value.status_code == 400

    @pytest.mark.asyncio
    async def test_the_repository_methods_take_only_the_accounts_they_are_given(
        self, migrated_db: DatabaseConnection, sides: tuple[Side, Side, str],
    ) -> None:
        a, b, word = sides
        repo = MessageRepository(migrated_db)
        tokens = await repo.tokenize(word)
        mine = frozenset({a.account_id})

        full_text = await repo.search_fulltext_with_snippet(mine, word, limit=50)
        assert {m.id for m, _snippet in full_text} == a.ids

        primary = await repo.search_messages(mine, tokens, limit=50)
        assert {m.id for m, _snippet, _tier in primary} == a.ids

        fuzzy = await repo.search_messages_fallback(mine, tokens, limit=50)
        assert {m.id for m, _snippet in fuzzy} == a.ids

        assert await repo.count_search_candidates(mine, tokens) == 2
        assert await repo.count_search_candidates(frozenset(), tokens) == 0
        assert await repo.search_fulltext_with_snippet(frozenset(), word) == []
        assert await repo.search_messages(frozenset(), tokens) == []
        assert await repo.resolve_search_cursor(mine, b.live_id, tokens) is None
        assert await repo.resolve_search_cursor(mine, a.live_id, tokens) is not None


class TestSemanticSearch:
    @_BOTH_PATHS
    @pytest.mark.asyncio
    async def test_the_same_vectors_in_two_accounts_are_found_only_in_the_callers(
        self, migrated_db: DatabaseConnection, exact_scan_max_rows: int,
    ) -> None:
        model = _unique_model()
        a_account, a_ids = await _seed_scored_pool(migrated_db, model, _SPREAD, _spread_dates())
        b_account, b_ids = await _seed_scored_pool(migrated_db, model, _SPREAD, _spread_dates())

        async def found(account_ids: list[uuid.UUID]) -> set[uuid.UUID]:
            results = await semantic_search(
                migrated_db, query_vector=_QUERY_VECTOR, model=model, account_ids=account_ids,
                exact_scan_max_rows=exact_scan_max_rows, k=10,
            )
            return {r.message.id for r in results}

        mine, theirs = await found([a_account]), await found([b_account])
        assert mine and theirs
        assert mine <= set(a_ids) and theirs <= set(b_ids)
        assert await found([a_account, b_account]) <= set(a_ids) | set(b_ids)
        assert await found([]) == set()

    @_BOTH_PATHS
    @pytest.mark.asyncio
    async def test_a_glaciered_message_of_another_account_is_not_reached_by_folder(
        self, migrated_db: DatabaseConnection, exact_scan_max_rows: int,
    ) -> None:
        model = _unique_model()
        a, b = await _seed_side(migrated_db, "x"), await _seed_side(migrated_db, "x")
        async with migrated_db.session() as session:
            for side in (a, b):
                await session.execute(
                    MessageEmbedding.__table__.insert().values(
                        account_id=side.account_id, msg_key=f"msg-{side.glacier_id}",
                        message_id=None, model=model, status="done", embedding=_QUERY_VECTOR,
                    )
                )

        results = await semantic_search(
            migrated_db, query_vector=_QUERY_VECTOR, model=model,
            account_ids=[a.account_id], folder_ids=[a.glacier_folder_id, b.glacier_folder_id],
            exact_scan_max_rows=exact_scan_max_rows, k=10,
        )

        assert {r.message.id for r in results} == {a.glacier_id}


class TestUnifiedViews:
    @pytest.mark.asyncio
    async def test_each_user_has_their_own_views_even_of_one_name(
        self, migrated_db: DatabaseConnection, sides: tuple[Side, Side, str],
    ) -> None:
        a, b, word = sides
        name = f"Inbox {word}"
        view_a = await create_unified_view(UnifiedViewCreate(name=name), scope=a.scope)
        view_b = await create_unified_view(UnifiedViewCreate(name=name), scope=b.scope)
        with pytest.raises(HTTPException) as duplicate:
            await create_unified_view(UnifiedViewCreate(name=name), scope=a.scope)
        assert duplicate.value.status_code == 409

        async with migrated_db.session() as session:
            await set_folder_views(session, a.folder_id, [view_a.id])
            await set_folder_views(session, b.folder_id, [view_b.id])

        seen_by_a = {v.id for v in await list_unified_folders(scope=a.scope)}
        assert view_a.id in seen_by_a and view_b.id not in seen_by_a
        async with migrated_db.session() as session:
            page_a = await list_message_page(
                session, account_id=None, folder_id=None,
                folder_scope=member_folder_ids(view_a.id, a.scope.account_ids),
                threaded=False, is_seen=None, since=None, before=None, after=None, around=None,
                limit=50,
            )
        assert {m.id for m in page_a.messages} == {a.live_id}

        listed = await list_unified_messages(
            folder_name=name, before=None, limit=50, threaded=False, is_seen=None, after=None,
            around=None, since=None, scope=b.scope,
        )
        assert {m.id for m in listed.messages} == {b.live_id}

    @pytest.mark.asyncio
    async def test_a_view_cannot_be_read_changed_or_deleted_by_another_user(
        self, migrated_db: DatabaseConnection, sides: tuple[Side, Side, str],
    ) -> None:
        a, b, word = sides
        view_b = await create_unified_view(UnifiedViewCreate(name=f"Private {word}"), scope=b.scope)

        for attempt in (
            update_unified_view(view_b.id, UnifiedViewUpdate(name="Taken"), scope=a.scope),
            delete_unified_view(view_b.id, scope=a.scope),
        ):
            with pytest.raises(HTTPException) as refused:
                await attempt
            assert refused.value.status_code == 403
        with pytest.raises(HTTPException) as unknown:
            await list_unified_messages(
                folder_name=view_b.name, before=None, limit=50, threaded=False, is_seen=None,
                after=None, around=None, since=None, scope=a.scope,
            )
        assert unknown.value.status_code == 404
        assert view_b.id in {v.id for v in await list_unified_folders(scope=b.scope)}

    @pytest.mark.asyncio
    async def test_a_folder_joins_only_its_owners_views(
        self, migrated_db: DatabaseConnection, sides: tuple[Side, Side, str],
    ) -> None:
        a, b, word = sides
        view_a = await create_unified_view(UnifiedViewCreate(name=f"A {word}"), scope=a.scope)
        view_b = await create_unified_view(UnifiedViewCreate(name=f"B {word}"), scope=b.scope)

        for folder, view in ((b.folder_id, view_a), (a.folder_id, view_b)):
            async with migrated_db.session() as session:
                with pytest.raises(HTTPException) as refused:
                    await set_folder_views(session, folder, [view.id])
            assert refused.value.status_code == 403
        async with migrated_db.session() as session:
            memberships = (await session.execute(
                text("SELECT count(*) FROM unified_view_folders WHERE view_id IN (:a, :b)"),
                {"a": view_a.id, "b": view_b.id},
            )).scalar_one()
        assert memberships == 0

    @pytest.mark.asyncio
    async def test_a_membership_naming_another_users_folder_shows_nothing_of_it(
        self, migrated_db: DatabaseConnection, sides: tuple[Side, Side, str],
    ) -> None:
        """A row a past owner left behind (or any path that wrote the table
        directly) must not widen the view."""
        a, b, word = sides
        view_a = await create_unified_view(UnifiedViewCreate(name=f"Mixed {word}"), scope=a.scope)
        async with migrated_db.session() as session:
            await session.execute(
                text("INSERT INTO unified_view_folders (view_id, folder_id) VALUES (:v, :f)"),
                {"v": view_a.id, "f": a.folder_id},
            )
            await session.execute(
                text("INSERT INTO unified_view_folders (view_id, folder_id) VALUES (:v, :f)"),
                {"v": view_a.id, "f": b.folder_id},
            )

        listed = await list_unified_messages(
            folder_name=view_a.name, before=None, limit=50, threaded=False, is_seen=None,
            after=None, around=None, since=None, scope=a.scope,
        )
        assert {m.id for m in listed.messages} == {a.live_id}
        folders = next(
            v for v in await list_unified_folders(scope=a.scope) if v.id == view_a.id
        ).folders
        assert {f.folder_id for f in folders} == {a.folder_id}
        async with migrated_db.session() as session:
            assert await view_ids_by_folder(session, [b.folder_id]) == {}
            assert await view_ids_by_folder(session, [a.folder_id]) == {a.folder_id: [view_a.id]}
        with pytest.raises(HTTPException) as cursor:
            await list_unified_messages(
                folder_name=view_a.name, before=b.live_id, limit=50, threaded=False, is_seen=None,
                after=None, around=None, since=None, scope=a.scope,
            )
        assert cursor.value.status_code == 400

        async with migrated_db.session() as session:
            await drop_account_memberships(session, b.account_id)
            left = (await session.execute(
                text("SELECT folder_id FROM unified_view_folders WHERE view_id = :v"),
                {"v": view_a.id},
            )).scalars().all()
        assert list(left) == [a.folder_id]

    @pytest.mark.asyncio
    async def test_giving_an_account_away_takes_its_folders_out_of_the_old_owners_views(
        self, migrated_db: DatabaseConnection, sides: tuple[Side, Side, str],
    ) -> None:
        a, b, word = sides
        view_a = await create_unified_view(UnifiedViewCreate(name=f"Kept {word}"), scope=a.scope)
        async with migrated_db.session() as session:
            await set_folder_views(session, a.folder_id, [view_a.id])

        async def members() -> list[uuid.UUID]:
            async with migrated_db.session() as session:
                return list((await session.execute(
                    text("SELECT folder_id FROM unified_view_folders WHERE view_id = :v"),
                    {"v": view_a.id},
                )).scalars().all())

        # Confirming the owner an account already has changes nothing.
        await set_owner(a.account_id, AdminOwnerRequest(user_id=a.user_id))
        assert await members() == [a.folder_id]

        await set_owner(a.account_id, AdminOwnerRequest(user_id=b.user_id))
        assert await members() == []

    @pytest.mark.asyncio
    async def test_a_list_naming_no_account_and_no_scope_is_refused(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            with pytest.raises(ValueError):
                await list_message_page(
                    session, account_id=None, folder_id=None, folder_scope=None,
                    threaded=False, is_seen=None, since=None, before=None, after=None,
                    around=None, limit=50,
                )


@pytest_asyncio.fixture()
async def databases(migrated_db: DatabaseConnection) -> AsyncIterator[None]:
    """The tools and the scope dependency read the database through their own
    module-level bindings of the connection."""
    targets = (
        "mail_verdict.api.mcp_tools.get_db_connection",
        "mail_verdict.api.deps.get_db_connection",
    )
    patchers = [patch(target, return_value=migrated_db) for target in targets]
    for p in patchers:
        p.start()
    try:
        yield
    finally:
        for p in patchers:
            p.stop()


@asynccontextmanager
async def acting_as(side: Side) -> AsyncIterator[Client]:
    """An in-memory MCP client acting as one user. The principal is bound
    before the client opens, so the server's tasks inherit it, and the scope
    is read from the database's own ownership rows."""
    principal = Principal(user_id=side.user_id, subject=f"test:{side.user_id}", is_admin=False)
    with as_principal(principal):
        async with Client(mcp) as client:
            yield client


class TestAssistantTools:
    @pytest.mark.asyncio
    async def test_a_tool_sees_the_callers_mail_and_nothing_else(
        self, sides: tuple[Side, Side, str], databases: None,
    ) -> None:
        a, b, word = sides
        async with acting_as(a) as client:
            await self._as_a(client, a, b, word)
        async with acting_as(b) as client:
            await self._as_b(client, b, word)

    @staticmethod
    async def _as_a(client: Client, a: Side, b: Side, word: str) -> None:

        accounts = (await client.call_tool("list_accounts", {})).data
        assert {x["id"] for x in accounts} == {str(a.account_id)}

        hits = (await client.call_tool("search_mail", {"query": word})).data
        assert {h["id"] for h in hits} == {str(i) for i in a.ids}
        named = (await client.call_tool(
            "search_mail", {"query": word, "account_id": str(b.account_id)},
        )).data
        assert named == []

        # B's ids and accounts, named directly: an error, never B's content.
        calls: list[tuple[str, dict[str, Any]]] = [
            ("get_mail", {"mail_id": str(b.live_id)}),
            ("get_mail", {"mail_id": str(b.glacier_id)}),
            ("get_thread", {"mail_id": str(b.live_id)}),
            ("list_mails", {"account_id": str(b.account_id)}),
            ("list_folders", {"account_id": str(b.account_id)}),
            ("get_verdict", {"mail_id": str(b.live_id)}),
            ("mark_mail", {"mail_id": str(b.live_id), "is_seen": True}),
            ("tag_mail", {"mail_id": str(b.live_id), "tag_name": "planted"}),
            ("move_mail", {"mail_id": str(b.live_id), "target_folder": "INBOX"}),
            ("get_stats", {"account_id": str(b.account_id)}),
            (
                "submit_spam_feedback",
                {"mail_id": str(b.live_id), "account_id": str(b.account_id), "is_spam": True},
            ),
            (
                "send_mail",
                {
                    "account_id": str(b.account_id), "to": ["x@example.com"],
                    "subject": "planted", "body_text": "planted",
                },
            ),
            (
                "draft_mail",
                {
                    "account_id": str(b.account_id), "to": ["x@example.com"],
                    "subject": "planted", "body_text": "planted",
                },
            ),
            ("reply_mail", {"mail_id": str(b.live_id), "mode": "reply", "body_text": "planted"}),
        ]
        for name, arguments in calls:
            result = (await client.call_tool(name, arguments)).data
            blob = str(result)
            assert word not in blob and "Live" not in blob, (name, blob)
            failed = (
                isinstance(result, dict) and ("error" in result or result.get("success") is False)
            ) or (isinstance(result, list) and result and "error" in result[0])
            assert failed and "forbidden" in blob, (name, result)


    @staticmethod
    async def _as_b(client: Client, b: Side, word: str) -> None:
        accounts = (await client.call_tool("list_accounts", {})).data
        assert {x["id"] for x in accounts} == {str(b.account_id)}
        own = (await client.call_tool("get_mail", {"mail_id": str(b.live_id)})).data
        assert own["subject"] == f"Live {word}"
        assert (await client.call_tool("get_thread", {"mail_id": str(b.live_id)})).data
