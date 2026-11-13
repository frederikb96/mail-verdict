"""
PipelineRevisionRepository against a real database: the optimistic-
concurrency guard of append() and the per-user history.

`pipeline_revisions` is append-only and shared with every other pg test
in this session (test_pipeline_api.py's own docstring says so), so every
test here writes as a user of its own and asserts only on that user's
history.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.pipeline.revisions import PipelineRevisionRepository, StaleRevisionError
from mail_verdict.pipeline.template import current_or_template, template_document
from tests.helpers.pipeline_pg import insert_user

_EMPTY_DOCUMENT = {"enabled": True, "stages": []}


@pytest.mark.asyncio
async def test_append_with_no_expectation_writes_unconditionally(
    migrated_db: DatabaseConnection,
) -> None:
    repo = PipelineRevisionRepository(migrated_db)
    user = await insert_user(migrated_db)
    first = await repo.append(user, _EMPTY_DOCUMENT, note="baseline")
    second = await repo.append(user, _EMPTY_DOCUMENT, note="unconditional")
    assert second > first


@pytest.mark.asyncio
async def test_append_with_a_correct_expectation_succeeds(
    migrated_db: DatabaseConnection,
) -> None:
    repo = PipelineRevisionRepository(migrated_db)
    user = await insert_user(migrated_db)
    baseline = await repo.append(user, _EMPTY_DOCUMENT, note="baseline")
    written = await repo.append(
        user, _EMPTY_DOCUMENT, note="matches base", expected_base_revision=baseline,
    )
    assert written > baseline


@pytest.mark.asyncio
async def test_append_with_a_stale_expectation_raises(migrated_db: DatabaseConnection) -> None:
    repo = PipelineRevisionRepository(migrated_db)
    user = await insert_user(migrated_db)
    baseline = await repo.append(user, _EMPTY_DOCUMENT, note="baseline")
    moved_on = await repo.append(user, _EMPTY_DOCUMENT, note="a concurrent writer moved on")

    with pytest.raises(StaleRevisionError) as exc_info:
        await repo.append(user, _EMPTY_DOCUMENT, note="stale", expected_base_revision=baseline)
    assert exc_info.value.expected == baseline
    assert exc_info.value.actual == moved_on


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
    the other gets StaleRevisionError -- and the user's history gains
    exactly one row, never two.
    """
    repo = PipelineRevisionRepository(migrated_db)
    user = await insert_user(migrated_db)
    baseline = await repo.append(user, _EMPTY_DOCUMENT, note="baseline")

    results = await asyncio.gather(
        repo.append(
            user, {"enabled": True, "stages": []}, note="writer-a",
            expected_base_revision=baseline,
        ),
        repo.append(
            user, {"enabled": False, "stages": []}, note="writer-b",
            expected_base_revision=baseline,
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 1, results
    assert len(failures) == 1, results
    assert isinstance(failures[0], StaleRevisionError)

    revisions = await repo.list_revisions(user)
    assert [r["revision"] for r in revisions] == [successes[0], baseline]


@pytest.mark.asyncio
async def test_each_user_has_their_own_history_and_current(
    migrated_db: DatabaseConnection,
) -> None:
    repo = PipelineRevisionRepository(migrated_db)
    a, b = await insert_user(migrated_db), await insert_user(migrated_db)
    a_rev = await repo.append(a, {"enabled": True, "stages": []}, note="a")
    b_rev = await repo.append(b, {"enabled": False, "stages": []}, note="b")

    a_current, b_current = await repo.current(a), await repo.current(b)
    assert a_current is not None and a_current.revision == a_rev and a_current.enabled
    assert b_current is not None and b_current.revision == b_rev and not b_current.enabled
    assert [r["revision"] for r in await repo.list_revisions(a)] == [a_rev]
    # A revision number is instance-wide, but only its author can read it.
    assert await repo.get(a, b_rev) is None
    assert (await repo.get(b, b_rev)) is not None


@pytest.mark.asyncio
async def test_a_base_revision_is_compared_with_the_authors_own_latest(
    migrated_db: DatabaseConnection,
) -> None:
    """Another user writing in between is not a conflict."""
    repo = PipelineRevisionRepository(migrated_db)
    a, b = await insert_user(migrated_db), await insert_user(migrated_db)
    base = await repo.append(a, _EMPTY_DOCUMENT, note="base")
    await repo.append(b, _EMPTY_DOCUMENT, note="someone else")
    await repo.append(a, _EMPTY_DOCUMENT, note="mine", expected_base_revision=base)


@pytest.mark.asyncio
async def test_a_user_with_no_document_gets_the_template_once(
    migrated_db: DatabaseConnection,
) -> None:
    repo = PipelineRevisionRepository(migrated_db)
    user = await insert_user(migrated_db)
    assert await repo.current(user) is None

    first, second = await asyncio.gather(
        current_or_template(repo, user), current_or_template(repo, user),
    )

    assert first.revision == second.revision
    assert len(await repo.list_revisions(user)) == 1
    assert [s.stage_id for s in first.stages] == [
        s["stage_id"] for s in template_document()["stages"]
    ]
    # Once written it is the user's, not regenerated.
    await repo.append(user, _EMPTY_DOCUMENT, note="their own")
    assert (await current_or_template(repo, user)).stages == ()


@pytest.mark.asyncio
async def test_the_summaries_are_counts_per_user(migrated_db: DatabaseConnection) -> None:
    repo = PipelineRevisionRepository(migrated_db)
    user = await insert_user(migrated_db)
    stage = {"type": "match", "config": {"when": {}, "effects": []}}
    revision = await repo.append(user, {
        "enabled": False, "stages": [
            {"stage_id": "a", **stage}, {"stage_id": "b", "enabled": False, **stage},
        ],
    }, note="x")

    summary = next(s for s in await repo.summaries() if s["user_id"] == user)

    assert summary["revision"] == revision
    assert (summary["enabled"], summary["stages"], summary["enabled_stages"]) == (False, 2, 1)
    assert uuid.UUID(str(summary["user_id"])) == user
