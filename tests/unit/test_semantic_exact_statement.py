"""The exact path ranks every vector in its scope: it is never ordered by the distance itself."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from mail_verdict.database.models import EMBEDDING_DIMENSIONS
from mail_verdict.embeddings.search import _exact_statement, _Scope


def _sql(glacier_folders: dict[uuid.UUID, uuid.UUID] | None) -> str:
    scope = _Scope(
        model="m", query_vector=[0.0] * EMBEDDING_DIMENSIONS, account_id=None, folder_ids=None,
        received_after=None, received_before=None,
    )
    return str(_exact_statement(scope, glacier_folders, 400).compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize(
    "glacier_folders", [None, {uuid.uuid4(): uuid.uuid4()}], ids=["live-only", "with-glacier"],
)
def test_the_ranking_orders_by_similarity_not_by_the_distance_operator(
    glacier_folders: dict[uuid.UUID, uuid.UUID] | None,
) -> None:
    """`ORDER BY embedding <=> q` is the one form the HNSW index can answer,
    which would make the exact path approximate; ordering by 1 - distance
    cannot be."""
    sql = _sql(glacier_folders)

    order_by = sql.rsplit("ORDER BY", 1)[1]
    assert "similarity DESC" in order_by
    assert "<=>" not in order_by
