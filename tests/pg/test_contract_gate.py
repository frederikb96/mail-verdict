"""
Proves the keystone against a real PostIMAP + Postgres:

- Alembic migrates MailVerdict's owned tables cleanly next to PostIMAP's
  own schema, regardless of which service migrated first.
- The contract-version handshake passes against a real postimap_info row.
- A version mismatch is fatal, not a silent degrade.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Account, Verdict, VerdictSource
from mail_verdict.postimap.contract import (
    SUPPORTED_CONTRACT_VERSION,
    ContractMismatchError,
    assert_contract_version,
    read_postimap_info,
)


@pytest.mark.asyncio
async def test_owned_tables_exist_after_migration(migrated_db: DatabaseConnection) -> None:
    """Every MailVerdict-owned table from the baseline migration is queryable.

    Row counts are not asserted here: postgres_container is session-scoped
    and shared across every test in this file, so other tests' rows may
    already be present regardless of execution order.
    """
    owned_tables = [
        "settings", "account_prefs", "folder_prefs",
        "verdicts", "mail_tags", "image_exceptions",
    ]
    async with migrated_db.session() as session:
        for table in owned_tables:
            result = await session.execute(text(f"SELECT count(*) FROM {table}"))
            assert result.scalar_one() >= 0


@pytest.mark.asyncio
async def test_postimap_tables_are_reachable_from_the_same_connection(
    migrated_db: DatabaseConnection,
) -> None:
    """PostIMAP's own migrations ran independently and are visible here too."""
    async with migrated_db.session() as session:
        result = await session.execute(select(Account.id))
        # Only proves the table is queryable through MailVerdict's own
        # mapped model -- not a claim about which rows other tests left.
        assert isinstance(result.all(), list)


@pytest.mark.asyncio
async def test_contract_version_matches(migrated_db: DatabaseConnection) -> None:
    """A real PostIMAP reports the exact contract_version this build expects."""
    async with migrated_db.session() as session:
        info = await read_postimap_info(session)

    assert info is not None
    assert info.contract_version == SUPPORTED_CONTRACT_VERSION
    assert_contract_version(info)  # must not raise


@pytest.mark.asyncio
async def test_contract_mismatch_is_fatal(migrated_db: DatabaseConnection) -> None:
    """A deliberately corrupted contract_version is refused, not tolerated."""
    async with migrated_db.session() as session:
        await session.execute(text("UPDATE postimap_info SET contract_version = 99"))

    try:
        async with migrated_db.session() as session:
            info = await read_postimap_info(session)

        assert info is not None
        with pytest.raises(ContractMismatchError):
            assert_contract_version(info)
    finally:
        # Restore real state -- postgres_container is session-scoped, so a
        # later test in this run would otherwise inherit the corruption.
        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE postimap_info SET contract_version = :v"),
                {"v": SUPPORTED_CONTRACT_VERSION},
            )


@pytest.mark.asyncio
async def test_verdict_row_has_no_foreign_key_onto_messages(
    migrated_db: DatabaseConnection,
) -> None:
    """A verdict can be inserted for a mail_id that names no existing message row.

    This is the load-bearing property behind durability across PostIMAP's
    retention purge: verdicts must be able to outlive the message row they
    were issued for.
    """
    async with migrated_db.session() as session:
        verdict = Verdict(
            mail_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            message_id_hdr="<never-existed@example.com>",
            msg_key="<never-existed@example.com>",
            is_spam=True,
            source=VerdictSource.AI,
        )
        session.add(verdict)
        await session.flush()
        await session.refresh(verdict)
        assert verdict.id is not None
