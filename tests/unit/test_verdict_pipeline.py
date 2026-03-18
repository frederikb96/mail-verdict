"""Tests for VerdictPipeline: full spam flow (mocked), folder-type awareness."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.database.models import SpecialUse
from mail_verdict.spam.analyst import SpamVerdict
from mail_verdict.spam.pipeline import _SKIP_FOLDER_TYPES, VerdictPipeline


def _make_config(enabled: bool = True) -> MagicMock:
    """Create a mock MailVerdictConfig."""
    config = MagicMock()
    config.spam.enabled = enabled
    config.spam.excerpt_length = 300
    config.spam.neighbor_count = 3
    config.spam.auto_mark_read = True
    config.ai.model = "test-model"
    return config


def _make_mail(
    from_addr: str = "alice@example.com",
    subject: str = "Test",
    body_text: str = "Hello world",
) -> MagicMock:
    """Create a mock Mail object."""
    mail = MagicMock()
    mail.id = uuid.uuid4()
    mail.account_id = uuid.uuid4()
    mail.uid = 42
    mail.from_addr = from_addr
    mail.subject = subject
    mail.body_text = body_text
    mail.to_addrs = {"addrs": ["bob@example.com"]}
    mail.cc_addrs = None
    mail.dkim_pass = True
    mail.spf_pass = True
    mail.dmarc_pass = True
    mail.received_at = None
    return mail


def _make_folder(special_use: SpecialUse | None = None) -> MagicMock:
    """Create a mock Folder."""
    folder = MagicMock()
    folder.id = uuid.uuid4()
    folder.imap_name = "INBOX"
    folder.special_use = special_use
    return folder


class TestSkipFolderTypes:
    """Tests for folder-type-based skipping."""

    def test_skip_sent(self) -> None:
        assert SpecialUse.SENT in _SKIP_FOLDER_TYPES

    def test_skip_drafts(self) -> None:
        assert SpecialUse.DRAFTS in _SKIP_FOLDER_TYPES

    def test_skip_trash(self) -> None:
        assert SpecialUse.TRASH in _SKIP_FOLDER_TYPES


class TestVerdictPipeline:
    """Tests for VerdictPipeline.process_mail."""

    def _make_pipeline(
        self,
        config: MagicMock | None = None,
        verdict_is_spam: bool = False,
    ) -> tuple[VerdictPipeline, dict[str, MagicMock]]:
        """Create pipeline with mock dependencies, return (pipeline, mocks)."""
        config = config or _make_config()

        store = MagicMock()
        store.upsert = AsyncMock(return_value=True)
        store.search = AsyncMock(return_value=[])
        store.update_payload = AsyncMock(return_value=True)

        analyst = MagicMock()
        analyst.analyze = AsyncMock(
            return_value=SpamVerdict(
                is_spam=verdict_is_spam,
                raw_response={"verdict": "spam" if verdict_is_spam else "not-spam"},
            )
        )

        verdict_repo = MagicMock()
        verdict_repo.create_verdict = AsyncMock()

        mail_repo = MagicMock()

        propagator = MagicMock()
        propagator.move_to_spam = AsyncMock(return_value=True)

        folder_repo = MagicMock()
        folder_repo.get_by_account = AsyncMock(return_value=[])

        pipeline = VerdictPipeline(
            config=config,
            semantic_store=store,
            analyst=analyst,
            verdict_repo=verdict_repo,
            mail_repo=mail_repo,
            action_propagator=propagator,
            folder_repo=folder_repo,
        )

        mocks = {
            "store": store,
            "analyst": analyst,
            "verdict_repo": verdict_repo,
            "propagator": propagator,
            "folder_repo": folder_repo,
        }
        return pipeline, mocks

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self) -> None:
        """Returns None when spam detection is disabled."""
        config = _make_config(enabled=False)
        pipeline, _ = self._make_pipeline(config=config)
        result = await pipeline.process_mail(_make_mail(), _make_folder())
        assert result is None

    @pytest.mark.asyncio
    async def test_skip_sent_folder(self) -> None:
        """Returns None for sent folder."""
        pipeline, _ = self._make_pipeline()
        result = await pipeline.process_mail(_make_mail(), _make_folder(SpecialUse.SENT))
        assert result is None

    @pytest.mark.asyncio
    async def test_skip_drafts_folder(self) -> None:
        """Returns None for drafts folder."""
        pipeline, _ = self._make_pipeline()
        result = await pipeline.process_mail(_make_mail(), _make_folder(SpecialUse.DRAFTS))
        assert result is None

    @pytest.mark.asyncio
    async def test_not_spam_flow(self) -> None:
        """Not-spam verdict returns False, stores verdict."""
        pipeline, mocks = self._make_pipeline(verdict_is_spam=False)
        result = await pipeline.process_mail(_make_mail(), _make_folder())
        assert result is False
        mocks["verdict_repo"].create_verdict.assert_awaited_once()
        mocks["propagator"].move_to_spam.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_spam_flow_moves_to_spam(self) -> None:
        """Spam verdict returns True and triggers move_to_spam."""
        pipeline, mocks = self._make_pipeline(verdict_is_spam=True)
        result = await pipeline.process_mail(_make_mail(), _make_folder())
        assert result is True
        mocks["propagator"].move_to_spam.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embed_failure_returns_none(self) -> None:
        """Returns None when embedding fails."""
        pipeline, mocks = self._make_pipeline()
        mocks["store"].upsert = AsyncMock(return_value=False)
        result = await pipeline.process_mail(_make_mail(), _make_folder())
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_body_skipped(self) -> None:
        """Mail with no embeddable text returns None."""
        pipeline, _ = self._make_pipeline()
        mail = _make_mail(from_addr="", subject="", body_text="")
        result = await pipeline.process_mail(mail, _make_folder())
        assert result is None
