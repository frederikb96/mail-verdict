"""
Search API: field-scoped prefix-tsquery matching, field-tier ranking, the
trigram fallback tier, server-side folder scoping, tiered keyset
pagination, and a latency proof at large-mailbox scale -- against a real
Postgres schema, since the tsquery construction, the per-field scoping and
the tier CASE all live in raw SQL this suite is the only thing that
exercises.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, Uuid, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.search import search_messages
from mail_verdict.database.connection import DatabaseConnection
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders
from tests.setup.large_mailbox import seed_large_mailbox_account

_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed_message(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    *,
    uid: int,
    subject: str | None = None,
    from_addr: str | None = None,
    to_addrs: list[str] | None = None,
    body_text: str | None = None,
    received_at: datetime | None = None,
) -> uuid.UUID:
    """A message carrying the columns search matches on -- the shared
    _seed_messages helper only fills is_seen, so this is its own local
    variant rather than widening a fixture other lanes share."""
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, "
            "subject, from_addr, to_addrs, body_text, received_at) "
            "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :msg_id, "
            ":subject, :from_addr, :to_addrs, :body_text, :received_at)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "uid": uid, "thread_id": uuid.uuid4(), "msg_id": f"<{message_id}@example.com>",
            "subject": subject, "from_addr": from_addr,
            "to_addrs": json.dumps(to_addrs) if to_addrs is not None else None,
            "body_text": body_text,
            "received_at": received_at or (_BASE_TIME + timedelta(minutes=uid)),
        },
    )
    return message_id


class TestFieldScoping:
    """Toggling subject/from/to/body actually changes what's searched --
    not filtered client-side afterward, computed in the query itself."""

    @pytest.mark.asyncio
    async def test_body_only_field_finds_body_text_not_subject(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            subject_hit = await _seed_message(
                session, account_id, inbox_id, uid=1,
                subject="quarterly report", body_text="nothing relevant",
            )
            body_hit = await _seed_message(
                session, account_id, inbox_id, uid=2,
                subject="hello", body_text="the quarterly report is attached",
            )
            await session.commit()

        page = await search_messages(
            q="quarterly", account_id=account_id, folder_ids=None,
            fields=["body"], before=None, limit=50,
        )
        ids = {r.id for r in page.results}
        assert ids == {body_hit}
        assert subject_hit not in ids

    @pytest.mark.asyncio
    async def test_to_field_matches_display_name_and_address(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            by_name = await _seed_message(
                session, account_id, inbox_id, uid=1,
                to_addrs=["Alice Example <alice@example.com>"],
            )
            by_addr = await _seed_message(
                session, account_id, inbox_id, uid=2,
                to_addrs=["someone else <bob@example.com>"],
            )
            unrelated = await _seed_message(
                session, account_id, inbox_id, uid=3, to_addrs=["nobody <nobody@example.com>"],
            )
            await session.commit()

        by_name_page = await search_messages(
            q="Alice", account_id=account_id, folder_ids=None, fields=["to"], before=None, limit=50,
        )
        assert {r.id for r in by_name_page.results} == {by_name}

        by_addr_page = await search_messages(
            q="bob@example.com", account_id=account_id, folder_ids=None,
            fields=["to"], before=None, limit=50,
        )
        assert {r.id for r in by_addr_page.results} == {by_addr}
        assert unrelated not in {r.id for r in by_addr_page.results}


class TestFuzzyMatching:
    """A single-character typo still finds the message -- the whole reason
    this isn't a plain ILIKE substring search."""

    @pytest.mark.asyncio
    async def test_typo_in_query_still_matches(self, migrated_db: DatabaseConnection) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            hit = await _seed_message(
                session, account_id, inbox_id, uid=1, subject="reimbursement request",
            )
            await session.commit()

        page = await search_messages(
            # "reimbursment" -- one letter dropped
            q="reimbursment", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )
        assert {r.id for r in page.results} == {hit}

    @pytest.mark.asyncio
    async def test_unrelated_query_finds_nothing(self, migrated_db: DatabaseConnection) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            await _seed_message(
                session, account_id, inbox_id, uid=1, subject="reimbursement request",
            )
            await session.commit()

        page = await search_messages(
            q="zzqqxxvv", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )
        assert page.results == []


class TestFolderScoping:
    """Enforced in the query itself -- a request naming one folder never
    sees a hit that lives in the other, however cheap it would be to
    filter client-side instead."""

    @pytest.mark.asyncio
    async def test_folder_ids_restricts_the_query(self, migrated_db: DatabaseConnection) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
            inbox_hit = await _seed_message(session, account_id, inbox_id, uid=1, subject="invoice")
            junk_hit = await _seed_message(session, account_id, junk_id, uid=1, subject="invoice")
            await session.commit()

        inbox_only = await search_messages(
            q="invoice", account_id=account_id, folder_ids=[inbox_id],
            fields=["subject"], before=None, limit=50,
        )
        assert {r.id for r in inbox_only.results} == {inbox_hit}

        both = await search_messages(
            q="invoice", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )
        assert {r.id for r in both.results} == {inbox_hit, junk_hit}


class TestOrderingAndPagination:
    """Always newest first, and a full keyset walk touches every match
    exactly once -- the case that actually decides the backend design."""

    @pytest.mark.asyncio
    async def test_results_are_newest_first(self, migrated_db: DatabaseConnection) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            older = await _seed_message(session, account_id, inbox_id, uid=1, subject="invoice")
            newer = await _seed_message(session, account_id, inbox_id, uid=2, subject="invoice")
            await session.commit()

        page = await search_messages(
            q="invoice", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )
        assert [r.id for r in page.results] == [newer, older]

    @pytest.mark.asyncio
    async def test_paging_through_cursor_visits_every_match_once(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            expected = {
                await _seed_message(session, account_id, inbox_id, uid=i, subject="invoice")
                for i in range(1, 8)
            }
            await session.commit()

        seen: list[uuid.UUID] = []
        cursor: uuid.UUID | None = None
        for _ in range(20):
            page = await search_messages(
                q="invoice", account_id=account_id, folder_ids=None,
                fields=["subject"], before=cursor, limit=3,
            )
            seen.extend(r.id for r in page.results)
            if not page.has_more:
                break
            assert page.next_cursor is not None
            cursor = uuid.UUID(page.next_cursor)

        assert set(seen) == expected
        assert len(seen) == len(expected)  # no row repeated across pages

    @pytest.mark.asyncio
    async def test_a_message_with_no_received_at_sorts_last_and_is_still_visited(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A message with no date header at all (no received_at to sort
        by) belongs at the bottom of a newest-first list -- not pinned
        above every dated result, which is where Postgres's own DESC
        default (NULLS FIRST) would otherwise put it."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            dated = await _seed_message(session, account_id, inbox_id, uid=1, subject="invoice")
            # Inserted directly -- _seed_message's own default substitutes
            # a received_at whenever None is passed, so it cannot produce
            # a genuinely NULL one.
            undated = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO messages "
                    "(id, account_id, folder_id, imap_uid, thread_id, message_id, "
                    "subject, received_at) "
                    "VALUES (:id, :account_id, :folder_id, 2, :thread_id, :msg_id, "
                    "'invoice', NULL)"
                ),
                {
                    "id": undated, "account_id": account_id, "folder_id": inbox_id,
                    "thread_id": uuid.uuid4(), "msg_id": f"<{undated}@example.com>",
                },
            )
            await session.commit()

        page = await search_messages(
            q="invoice", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=1,
        )
        assert [r.id for r in page.results] == [dated]
        assert page.has_more and page.next_cursor is not None

        next_page = await search_messages(
            q="invoice", account_id=account_id, folder_ids=None,
            fields=["subject"], before=uuid.UUID(page.next_cursor), limit=1,
        )
        assert [r.id for r in next_page.results] == [undated]
        assert not next_page.has_more


class TestAccountScoping:
    @pytest.mark.asyncio
    async def test_account_id_excludes_other_accounts(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_a, inbox_a, _junk_a = await _seed_account_two_folders(session)
            account_b, inbox_b, _junk_b = await _seed_account_two_folders(session)
            own = await _seed_message(session, account_a, inbox_a, uid=1, subject="invoice")
            await _seed_message(session, account_b, inbox_b, uid=1, subject="invoice")
            await session.commit()

        page = await search_messages(
            q="invoice", account_id=account_a, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )
        assert {r.id for r in page.results} == {own}


class TestScale:
    """A body search across a mailbox large enough to matter, with the one
    matching message deliberately the oldest row (last found by a naive
    scan) -- proving the query returns quickly, not that it returns
    quickly in the specific case that would flatter it."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_body_search_over_thousands_of_messages(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        count = 3000
        async with migrated_db.session() as session:
            account_id, folder_id = await seed_large_mailbox_account(session)
            await session.commit()

        # One bulk INSERT, same shape tests/setup/large_mailbox.py uses --
        # count individual awaited INSERTs would each pay PostIMAP's own
        # per-row AFTER-trigger cost serially and blow well past a sane
        # test timeout long before the search query itself is measured.
        table = Table(
            "messages", MetaData(),
            Column("id", Uuid), Column("account_id", Uuid), Column("folder_id", Uuid),
            Column("imap_uid", BigInteger), Column("thread_id", Uuid), Column("message_id", Text),
            Column("subject", Text), Column("from_addr", Text), Column("body_text", Text),
            Column("received_at", DateTime(timezone=True)),
        )
        body = (
            "Thanks for subscribing. This week's update covers a handful of "
            "topics that have nothing to do with the marker phrase at all, "
            "padded out to a realistic message length for a scan to cost "
            "something real per row rather than being trivially short."
        )
        rows = [
            {
                "id": uuid.uuid4(), "account_id": account_id, "folder_id": folder_id,
                "imap_uid": 100 + i, "thread_id": uuid.uuid4(),
                "message_id": f"<{uuid.uuid4()}@large-mailbox.example.com>",
                "subject": f"Newsletter {i}", "from_addr": f"sender{i % 50}@example.com",
                "body_text": body, "received_at": _BASE_TIME + timedelta(minutes=i + 1),
            }
            for i in range(count)
        ]
        async with migrated_db.session() as session:
            marker_id = await _seed_message(
                session, account_id, folder_id, uid=1,
                subject="old newsletter", body_text="a rare marker phrase lives here: zzxqvantage",
                received_at=_BASE_TIME,
            )
            await session.execute(insert(table), rows)
            await session.commit()

        started = time.monotonic()
        page = await search_messages(
            q="zzxqvantage", account_id=account_id, folder_ids=None,
            fields=["body"], before=None, limit=50,
        )
        elapsed = time.monotonic() - started

        assert {r.id for r in page.results} == {marker_id}
        # Generous bound for a committed regression test on shared CI
        # hardware -- see the search agent's report for the real number
        # measured against this same query at full mailbox scale.
        print(f"\nbody search over {count} messages: {elapsed*1000:.1f}ms")
        assert elapsed < 5.0, f"body search over {count} messages took {elapsed:.2f}s"


class TestTierRanking:
    """Ranked by field tier, not date and not ts_rank -- search_vector
    carries no per-field weights, so ts_rank cannot express this."""

    @pytest.mark.asyncio
    async def test_subject_then_from_then_body_in_that_order(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            subject_hit = await _seed_message(
                session, account_id, inbox_id, uid=1,
                subject="zzqtierword report", from_addr="normal@example.com",
                body_text="unrelated",
            )
            from_hit = await _seed_message(
                session, account_id, inbox_id, uid=2,
                subject="unrelated subject", from_addr="zzqtierword@example.com",
                body_text="unrelated",
            )
            body_hit = await _seed_message(
                session, account_id, inbox_id, uid=3,
                subject="unrelated subject", from_addr="normal@example.com",
                body_text="the zzqtierword appears here",
            )
            await session.commit()

        page = await search_messages(
            q="zzqtierword", account_id=account_id, folder_ids=None,
            fields=["subject", "from", "to", "body"], before=None, limit=50,
        )
        assert [r.id for r in page.results] == [subject_hit, from_hit, body_hit]
        assert [r.match_tier for r in page.results] == [0, 1, 3]


class TestFallbackTier:
    """The trigram fallback only ever fires when the primary tsquery
    stage's first page came back empty -- an exact hit must never let an
    unrelated near-miss surface alongside it from the fallback, and a
    genuine typo must still find its match, at the fallback's own tier."""

    @pytest.mark.asyncio
    async def test_fallback_does_not_fire_when_primary_has_hits(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            exact_hit = await _seed_message(session, account_id, inbox_id, uid=1, subject="invoice")
            # word_similarity-eligible under the fallback tier, but not a
            # primary prefix match for "invoice" -- "invoic" is not a
            # prefix of "invoice", it is the other way around.
            near_miss = await _seed_message(
                session, account_id, inbox_id, uid=2, subject="a completely unrelated invoic",
            )
            await session.commit()

        page = await search_messages(
            q="invoice", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )
        ids = {r.id for r in page.results}
        assert exact_hit in ids
        assert near_miss not in ids
        assert all(r.match_tier != 4 for r in page.results)

    @pytest.mark.asyncio
    async def test_fallback_fires_on_a_typo_at_its_own_tier(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            hit = await _seed_message(
                session, account_id, inbox_id, uid=1, subject="reimbursement request",
            )
            await session.commit()

        page = await search_messages(
            q="reimbursment", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )
        assert {r.id for r in page.results} == {hit}
        assert page.results[0].match_tier == 4
        assert page.total == 1


class TestTsqueryInjection:
    """Raw user text is tokenized through Postgres's own parser before it
    ever reaches tsquery syntax -- a query built entirely of tsquery
    metacharacters must not error, and must simply match nothing it has
    no lexeme in common with."""

    @pytest.mark.asyncio
    async def test_pure_punctuation_query_finds_nothing_and_does_not_error(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            await _seed_message(session, account_id, inbox_id, uid=1, subject="invoice")
            await session.commit()

        page = await search_messages(
            q="!!!", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )
        assert page.results == []
        assert page.total == 0

    @pytest.mark.asyncio
    async def test_query_containing_tsquery_operator_characters_does_not_error(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            await _seed_message(session, account_id, inbox_id, uid=1, subject="a b c")
            await session.commit()

        # Every extracted lexeme is required (an AND), and this subject
        # has none of d/e/f -- an empty, error-free result is the correct
        # answer, not a crash from a raw '&', '|', '!', ':' or "'"
        # reaching tsquery syntax directly.
        page = await search_messages(
            q="a&b | c ! d:e ' f", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=50,
        )
        assert page.results == []


class TestTotalCount:
    """total is an exact count over the same candidate predicate the page
    itself pages through -- computed once, not derived from how many
    pages have loaded so far."""

    @pytest.mark.asyncio
    async def test_total_is_the_full_match_count_not_the_page_size(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            for i in range(5):
                await _seed_message(session, account_id, inbox_id, uid=i + 1, subject="invoice")
            await session.commit()

        page = await search_messages(
            q="invoice", account_id=account_id, folder_ids=None,
            fields=["subject"], before=None, limit=2,
        )
        assert len(page.results) == 2
        assert page.total == 5


class TestKeysetAcrossTiers:
    """A cursor carries a row's tier alongside (received_at, id) -- paging
    across a tier boundary must neither repeat nor skip a row."""

    @pytest.mark.asyncio
    async def test_paging_across_a_tier_boundary_visits_every_match_once(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            expected = {
                # Tier 0: token in subject.
                await _seed_message(
                    session, account_id, inbox_id, uid=1, subject="zzqcross report",
                ),
                await _seed_message(session, account_id, inbox_id, uid=2, subject="zzqcross memo"),
                # Tier 1: token only in from_addr.
                await _seed_message(
                    session, account_id, inbox_id, uid=3,
                    subject="x", from_addr="zzqcross@example.com",
                ),
                # Tier 3: token only in body.
                await _seed_message(
                    session, account_id, inbox_id, uid=4,
                    subject="x", body_text="zzqcross mentioned here",
                ),
                await _seed_message(
                    session, account_id, inbox_id, uid=5,
                    subject="x", body_text="zzqcross again",
                ),
            }
            await session.commit()

        seen: list[uuid.UUID] = []
        cursor: uuid.UUID | None = None
        for _ in range(20):
            page = await search_messages(
                q="zzqcross", account_id=account_id, folder_ids=None,
                fields=["subject", "from", "body"], before=cursor, limit=2,
            )
            seen.extend(r.id for r in page.results)
            if not page.has_more:
                break
            assert page.next_cursor is not None
            cursor = uuid.UUID(page.next_cursor)

        assert set(seen) == expected
        assert len(seen) == len(expected)  # no row repeated across pages
