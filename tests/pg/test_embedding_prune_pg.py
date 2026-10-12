"""
Pruning the embeddings of superseded models: only rows of a model that is
neither the active nor the target one go, only once the target has taken
over, and only when the configuration says so.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import MessageEmbedding
from mail_verdict.embeddings.repository import EmbeddingRepository
from mail_verdict.embeddings.worker import _maybe_prune
from tests.pg.test_message_embeddings import _unique_model


class _Settings:
    def __init__(self, **semantic: object) -> None:
        self._semantic = semantic

    def get(self, category: str) -> dict[str, object]:
        return self._semantic


async def _seed(
    db: DatabaseConnection, *, active: str, target: str, old: str,
) -> dict[str, list[uuid.UUID]]:
    """Two done rows per model, plus a claimed row of the old model."""
    account_id = uuid.uuid4()
    ids: dict[str, list[uuid.UUID]] = {model: [] for model in (active, target, old, "claimed")}
    async with db.session() as session:
        for model in dict.fromkeys((active, target, old)):
            for i in range(2):
                row_id = uuid.uuid4()
                ids[model].append(row_id)
                await session.execute(
                    MessageEmbedding.__table__.insert().values(
                        id=row_id, account_id=account_id, msg_key=f"{model}-{i}", model=model,
                        status="done", embedding=[1.0] + [0.0] * 1535,
                    )
                )
        claimed_id = uuid.uuid4()
        ids["claimed"].append(claimed_id)
        await session.execute(
            MessageEmbedding.__table__.insert().values(
                id=claimed_id, account_id=account_id, msg_key="claimed", model=old,
                status="claimed",
            )
        )
    return ids


async def _present(db: DatabaseConnection, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    async with db.session() as session:
        rows = await session.execute(
            text("SELECT id FROM message_embeddings WHERE id = ANY(:ids)"), {"ids": ids},
        )
        return {row.id for row in rows}


@pytest.mark.asyncio
async def test_only_rows_of_models_not_kept_go_and_a_claimed_one_stays(
    migrated_db: DatabaseConnection,
) -> None:
    active, target, old = _unique_model(), _unique_model(), _unique_model()
    ids = await _seed(migrated_db, active=active, target=target, old=old)

    deleted = await EmbeddingRepository(migrated_db).prune_models_other_than(
        {active, target}, batch_size=1,
    )

    assert deleted >= 2  # the old model's done rows (and any other test's leftovers)
    assert await _present(migrated_db, ids[old]) == set()
    assert await _present(migrated_db, ids[active]) == set(ids[active])
    assert await _present(migrated_db, ids[target]) == set(ids[target])
    assert await _present(migrated_db, ids["claimed"]) == set(ids["claimed"])


@pytest.mark.asyncio
async def test_enabled_and_the_target_active_deletes_the_superseded_model(
    migrated_db: DatabaseConnection,
) -> None:
    target, old = _unique_model(), _unique_model()
    ids = await _seed(migrated_db, active=target, target=target, old=old)
    settings = _Settings(model=target, active_model=target)

    await _maybe_prune(EmbeddingRepository(migrated_db), settings, target, enabled=True)  # type: ignore[arg-type]

    assert await _present(migrated_db, ids[old]) == set()
    assert await _present(migrated_db, ids[target]) == set(ids[target])


@pytest.mark.asyncio
async def test_disabled_deletes_nothing(migrated_db: DatabaseConnection) -> None:
    target, old = _unique_model(), _unique_model()
    ids = await _seed(migrated_db, active=target, target=target, old=old)
    settings = _Settings(model=target, active_model=target)

    await _maybe_prune(EmbeddingRepository(migrated_db), settings, target, enabled=False)  # type: ignore[arg-type]

    assert await _present(migrated_db, ids[old]) == set(ids[old])


@pytest.mark.asyncio
async def test_while_a_migration_is_in_flight_nothing_is_deleted(
    migrated_db: DatabaseConnection,
) -> None:
    """The active model is still the previous one, so its rows are what
    search answers from; the target is still filling."""
    active, target, old = _unique_model(), _unique_model(), _unique_model()
    ids = await _seed(migrated_db, active=active, target=target, old=old)
    settings = _Settings(model=target, active_model=active)

    await _maybe_prune(EmbeddingRepository(migrated_db), settings, target, enabled=True)  # type: ignore[arg-type]

    assert await _present(migrated_db, ids[old]) == set(ids[old])
    assert await _present(migrated_db, ids[active]) == set(ids[active])
