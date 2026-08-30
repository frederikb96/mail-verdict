"""
PipelineRevisionRepository.append()'s optimistic-concurrency guard
against a real database.

`pipeline_revisions` is append-only and shared with every other pg test
in this session (test_pipeline_api.py's own docstring says so), so every
test here establishes its own baseline with an unconditional append
before asserting anything.
"""

from __future__ import annotations

import asyncio

import pytest

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.pipeline.revisions import PipelineRevisionRepository, StaleRevisionError

_EMPTY_DOCUMENT = {"enabled": True, "stages": []}


@pytest.mark.asyncio
async def test_append_with_no_expectation_writes_unconditionally(
    migrated_db: DatabaseConnection,
) -> None:
    repo = PipelineRevisionRepository(migrated_db)
    first = await repo.append(_EMPTY_DOCUMENT, note="baseline")
    second = await repo.append(_EMPTY_DOCUMENT, note="unconditional")
    assert second == first + 1


@pytest.mark.asyncio
async def test_append_with_a_correct_expectation_succeeds(
    migrated_db: DatabaseConnection,
) -> None:
    repo = PipelineRevisionRepository(migrated_db)
    baseline = await repo.append(_EMPTY_DOCUMENT, note="baseline")
    written = await repo.append(
        _EMPTY_DOCUMENT, note="matches base", expected_base_revision=baseline,
    )
    assert written == baseline + 1


@pytest.mark.asyncio
async def test_append_with_a_stale_expectation_raises(migrated_db: DatabaseConnection) -> None:
    repo = PipelineRevisionRepository(migrated_db)
    baseline = await repo.append(_EMPTY_DOCUMENT, note="baseline")
    await repo.append(_EMPTY_DOCUMENT, note="a concurrent writer moved on")

    with pytest.raises(StaleRevisionError) as exc_info:
        await repo.append(_EMPTY_DOCUMENT, note="stale", expected_base_revision=baseline)
    assert exc_info.value.expected == baseline
    assert exc_info.value.actual == baseline + 1


@pytest.mark.asyncio
async def test_two_concurrent_writers_on_the_same_base_do_not_both_win(
    migrated_db: DatabaseConnection,
) -> None:
    """
    The race the 409 exists to prevent: two writers both read revision N
    and both call append() with expected_base_revision=N. Reading the
    current revision and appending in separate sessions -- the original
    bug -- lets both checks pass and both inserts happen, so the second
    writer's insert silently wins and the first writer never learns its
    edit was discarded. With the check and the insert in one transaction,
    serialized by an advisory lock, exactly one of the two succeeds and
    the other gets StaleRevisionError -- and the table gains exactly one
    row, never two.
    """
    repo = PipelineRevisionRepository(migrated_db)
    baseline = await repo.append(_EMPTY_DOCUMENT, note="baseline")

    results = await asyncio.gather(
        repo.append(
            {"enabled": True, "stages": []}, note="writer-a", expected_base_revision=baseline,
        ),
        repo.append(
            {"enabled": False, "stages": []}, note="writer-b", expected_base_revision=baseline,
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 1, results
    assert len(failures) == 1, results
    assert isinstance(failures[0], StaleRevisionError)
    assert successes[0] == baseline + 1

    revisions = await repo.list_revisions()
    assert len(revisions) == baseline + 1  # revision is a 1-based IDENTITY column
