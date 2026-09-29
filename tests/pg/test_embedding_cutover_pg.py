"""
_maybe_cutover: once EmbeddingRepository.cutover_readiness says the
target model is ready, active_model/active_provider/active_base_url must
all advance together, or a fresh search query gets embedded through the
wrong provider against the model that IS now active -- see
embeddings/provider.py's resolve_active_embedding_provider and
embeddings/worker.py's own docstring for why the three travel as one
identity.

Real coverage (EmbeddingRepository.status()) is exercised in
tests/pg/test_message_embeddings.py; it is stubbed here rather than
built from seeded messages, since status() counts in-scope messages
across every account in the shared migrated_db, not just this test's
own -- a real seed would be reading coverage polluted by whatever every
other pg test in the same session already left behind. cutover_readiness
itself is NOT reimplemented here: the stub delegates to the real method
(bound against the stub's own status()), so these tests exercise the
actual predicate rather than a second copy of it.
"""

from __future__ import annotations

import pytest

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.embeddings.repository import EmbeddingRepository, EmbeddingStatus
from mail_verdict.embeddings.worker import _maybe_cutover
from mail_verdict.settings.service import SettingsService


class _StubEmbeddingRepo:
    """Only status() is faked; cutover_readiness is the real
    implementation, bound to this stub so it reads the faked statuses."""

    def __init__(self, statuses: dict[str, EmbeddingStatus]) -> None:
        self._statuses = statuses

    async def status(self, *, model: str, account_id: object = None) -> EmbeddingStatus:
        return self._statuses[model]

    cutover_readiness = EmbeddingRepository.cutover_readiness


def _status(
    *, in_scope: int, encoded: int, reachable: int, pending: int = 0, failed: int = 0,
) -> EmbeddingStatus:
    return EmbeddingStatus(
        model="irrelevant", in_scope=in_scope, encoded=encoded, pending=pending, failed=failed,
        reachable=reachable, unreachable=encoded - reachable, shadowed=0,
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
    old_model = "old-model"
    await settings_service.update(
        "semantic",
        {
            "model": new_model, "provider": "custom", "base_url": "https://example.test/v1",
            "active_model": old_model, "active_provider": "openai", "active_base_url": None,
        },
    )

    repo = _StubEmbeddingRepo({
        new_model: _status(in_scope=5, encoded=5, reachable=5),
        old_model: _status(in_scope=5, encoded=5, reachable=5),
    })
    await _maybe_cutover(repo, settings_service, new_model)  # type: ignore[arg-type]

    semantic = settings_service.get("semantic")
    assert semantic["active_model"] == new_model
    assert semantic["active_provider"] == "custom"
    assert semantic["active_base_url"] == "https://example.test/v1"


@pytest.mark.asyncio
async def test_cutover_is_a_no_op_while_messages_remain_untried(
    migrated_db: DatabaseConnection,
) -> None:
    """Search must keep answering from the old identity for as long as
    even one in-scope message hasn't been tried under the target yet."""
    settings_service = SettingsService(migrated_db)
    await settings_service.load()

    new_model, old_model = "new-model", "old-model"
    await settings_service.update(
        "semantic", {"model": new_model, "active_model": old_model},
    )

    repo = _StubEmbeddingRepo({
        # encoded+pending+failed = 4, short of in_scope=5 -- one message
        # has neither succeeded, failed, nor even been claimed yet.
        new_model: _status(in_scope=5, encoded=4, reachable=4),
        old_model: _status(in_scope=5, encoded=5, reachable=5),
    })
    await _maybe_cutover(repo, settings_service, new_model)  # type: ignore[arg-type]

    assert settings_service.get("semantic")["active_model"] == old_model


@pytest.mark.asyncio
async def test_cutover_proceeds_despite_permanent_failures_that_will_never_clear(
    migrated_db: DatabaseConnection,
) -> None:
    """The actual gap this whole mechanism exists to close: a real
    mailbox always has a few messages that permanently fail to embed
    (no usable content, a provider refusal), so coverage == 1.0 is
    unreachable and a check waiting for it blocks forever. Readiness
    instead only needs everything *tried* and the target's reachable
    count to match the active model's."""
    settings_service = SettingsService(migrated_db)
    await settings_service.load()

    new_model, old_model = "new-model", "old-model"
    await settings_service.update(
        "semantic", {"model": new_model, "active_model": old_model},
    )

    repo = _StubEmbeddingRepo({
        # 2 of 5 permanently failed; the other 3 done -- nothing pending,
        # nothing untried, coverage is 0.6 and never reaches 1.0.
        new_model: _status(in_scope=5, encoded=3, reachable=3, failed=2),
        old_model: _status(in_scope=5, encoded=3, reachable=3, failed=2),
    })
    await _maybe_cutover(repo, settings_service, new_model)  # type: ignore[arg-type]

    assert settings_service.get("semantic")["active_model"] == new_model


@pytest.mark.asyncio
async def test_cutover_is_a_no_op_when_the_target_regresses_on_reachable_messages(
    migrated_db: DatabaseConnection,
) -> None:
    """Every message has been tried, but the target reaches fewer of
    them than the active model already does -- a real regression, not a
    message that was always going to fail. Must not cut over."""
    settings_service = SettingsService(migrated_db)
    await settings_service.load()

    new_model, old_model = "new-model", "old-model"
    await settings_service.update(
        "semantic", {"model": new_model, "active_model": old_model},
    )

    repo = _StubEmbeddingRepo({
        new_model: _status(in_scope=5, encoded=3, reachable=3, failed=2),
        old_model: _status(in_scope=5, encoded=5, reachable=5),
    })
    await _maybe_cutover(repo, settings_service, new_model)  # type: ignore[arg-type]

    assert settings_service.get("semantic")["active_model"] == old_model


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

    repo = _StubEmbeddingRepo({})
    repo.status = _boom  # type: ignore[method-assign]
    await _maybe_cutover(repo, settings_service, "same-model")  # type: ignore[arg-type]

    assert settings_service.get("semantic")["active_provider"] == "custom"
