"""
_maybe_cutover: once a re-embed's target model reaches full coverage,
active_model/active_provider/active_base_url must all advance together,
or a fresh search query gets embedded through the wrong provider against
the model that IS now active -- see embeddings/provider.py's
resolve_active_embedding_provider and embeddings/worker.py's own
docstring for why the three travel as one identity.

Coverage itself (EmbeddingRepository.status()) is exercised for real in
tests/pg/test_message_embeddings.py; it is stubbed here rather than
built from seeded messages, since status() counts in-scope messages
across every account in the shared migrated_db, not just this test's
own -- a real seed would be reading coverage polluted by whatever every
other pg test in the same session already left behind.
"""

from __future__ import annotations

import pytest

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.embeddings.repository import EmbeddingStatus
from mail_verdict.embeddings.worker import _maybe_cutover
from mail_verdict.settings.service import SettingsService


class _StubEmbeddingRepo:
    """Only the one method _maybe_cutover calls, returning a status this
    test controls directly rather than one built from real coverage."""

    def __init__(self, coverage_status: EmbeddingStatus) -> None:
        self._status = coverage_status

    async def status(self, *, model: str, account_id: object = None) -> EmbeddingStatus:
        return self._status


def _status(*, in_scope: int, reachable: int) -> EmbeddingStatus:
    return EmbeddingStatus(
        model="irrelevant", in_scope=in_scope, encoded=reachable, pending=0, failed=0,
        reachable=reachable, unreachable=0, shadowed=0,
    )


@pytest.mark.asyncio
async def test_cutover_advances_provider_and_base_url_together_with_model(
    migrated_db: DatabaseConnection,
) -> None:
    """The actual bug this reproduces: a migration that also changes
    provider (moving to a different compatible server, not just a new
    model name on the same one) must not leave active_provider/
    active_base_url pointing at the OLD provider once active_model has
    already advanced to the NEW one."""
    settings_service = SettingsService(migrated_db)
    await settings_service.load()

    # The frozen identity a provider-and-model change leaves behind --
    # settings_api.py's own freeze (_freeze_active_embedding_identity)
    # produces exactly this shape; this test drives the settings service
    # directly rather than going through the API layer.
    new_model = "new-model"
    await settings_service.update(
        "semantic",
        {
            "model": new_model, "provider": "custom", "base_url": "https://example.test/v1",
            "active_model": "old-model", "active_provider": "openai", "active_base_url": None,
        },
    )

    repo = _StubEmbeddingRepo(_status(in_scope=5, reachable=5))
    await _maybe_cutover(repo, settings_service, new_model)  # type: ignore[arg-type]

    semantic = settings_service.get("semantic")
    assert semantic["active_model"] == new_model
    assert semantic["active_provider"] == "custom"
    assert semantic["active_base_url"] == "https://example.test/v1"


@pytest.mark.asyncio
async def test_cutover_is_a_no_op_while_coverage_is_incomplete(
    migrated_db: DatabaseConnection,
) -> None:
    """Search must keep answering from the old identity for as long as
    even one in-scope message is missing a vector under the target."""
    settings_service = SettingsService(migrated_db)
    await settings_service.load()

    await settings_service.update(
        "semantic", {"model": "new-model", "active_model": "old-model"},
    )

    repo = _StubEmbeddingRepo(_status(in_scope=5, reachable=4))
    await _maybe_cutover(repo, settings_service, "new-model")  # type: ignore[arg-type]

    assert settings_service.get("semantic")["active_model"] == "old-model"


@pytest.mark.asyncio
async def test_cutover_is_a_no_op_once_already_advanced(
    migrated_db: DatabaseConnection,
) -> None:
    """A second reconciler tick after cutover already happened must not
    re-write settings on every pass -- there is nothing left to advance."""
    settings_service = SettingsService(migrated_db)
    await settings_service.load()

    await settings_service.update(
        "semantic",
        {"model": "same-model", "active_model": "same-model", "active_provider": "custom"},
    )

    async def _boom(**_kwargs: object) -> EmbeddingStatus:
        raise AssertionError("status() must not be called once already at the target")

    repo = _StubEmbeddingRepo(_status(in_scope=5, reachable=5))
    repo.status = _boom  # type: ignore[method-assign]
    await _maybe_cutover(repo, settings_service, "same-model")  # type: ignore[arg-type]

    assert settings_service.get("semantic")["active_provider"] == "custom"
