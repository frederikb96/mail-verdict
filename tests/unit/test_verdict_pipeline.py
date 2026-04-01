"""Tests for VerdictPipeline: full spam flow (mocked), folder-type awareness."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mail_verdict.spam.analyst import SpamVerdict
from mail_verdict.spam.pipeline import _SKIP_FOLDER_TYPES, VerdictPipeline


def _make_settings_service(enabled: bool = True) -> MagicMock:
    """Create a mock SettingsService."""
    service = MagicMock()
    service.get = MagicMock(side_effect=lambda cat: {
        "spam": {
            "enabled": enabled,
            "excerpt_length": 300,
            "neighbor_count": 3,
            "auto_mark_read": True,
        },
        "ai": {"model": "test-model"},
    }.get(cat, {}))
    return service


_TEST_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_message(
    from_addr: str = "alice@example.com",
    subject: str = "Test",
    body_text: str = "Hello world",
) -> MagicMock:
    """Create a mock Message object."""
    msg = MagicMock()
    msg.id = uuid.uuid4()
    msg.account_id = _TEST_ACCOUNT_ID
    msg.imap_uid = 42
    msg.from_addr = from_addr
    msg.subject = subject
    msg.body_text = body_text
    msg.to_addrs = {"addrs": ["bob@example.com"]}
    msg.cc_addrs = None
    msg.raw_headers = {
        "authentication-results": "dkim=pass; spf=pass; dmarc=pass",
    }
    msg.received_at = None
    return msg


def _make_folder(special_use: str | None = None) -> MagicMock:
    """Create a mock Folder."""
    folder = MagicMock()
    folder.id = uuid.uuid4()
    folder.imap_name = "INBOX"
    folder.special_use = special_use
    return folder


class TestSkipFolderTypes:
    """Tests for folder-type-based skipping."""

    def test_skip_sent(self) -> None:
        assert "sent" in _SKIP_FOLDER_TYPES

    def test_skip_drafts(self) -> None:
        assert "drafts" in _SKIP_FOLDER_TYPES

    def test_skip_trash(self) -> None:
        assert "trash" in _SKIP_FOLDER_TYPES


class TestVerdictPipeline:
    """Tests for VerdictPipeline.process_message (PostIMAP mode, direct SQL UPDATEs)."""

    def _make_pipeline(
        self,
        settings_service: MagicMock | None = None,
        verdict_is_spam: bool = False,
    ) -> tuple[VerdictPipeline, dict[str, MagicMock]]:
        """Create pipeline with mock dependencies, return (pipeline, mocks)."""
        settings_service = settings_service or _make_settings_service()

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

        message_repo = MagicMock()

        folder_repo = MagicMock()
        folder_repo.get_by_account = AsyncMock(return_value=[])

        pipeline = VerdictPipeline(
            settings_service=settings_service,
            semantic_store=store,
            analyst=analyst,
            verdict_repo=verdict_repo,
            message_repo=message_repo,
            folder_repo=folder_repo,
        )

        mocks = {
            "store": store,
            "analyst": analyst,
            "verdict_repo": verdict_repo,
            "folder_repo": folder_repo,
        }
        return pipeline, mocks

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self) -> None:
        """Returns None when spam detection is disabled."""
        svc = _make_settings_service(enabled=False)
        pipeline, _ = self._make_pipeline(settings_service=svc)
        result = await pipeline.process_message(_make_message(), _make_folder())
        assert result is None

    @pytest.mark.asyncio
    async def test_skip_sent_folder(self) -> None:
        """Returns None for sent folder."""
        pipeline, _ = self._make_pipeline()
        result = await pipeline.process_message(_make_message(), _make_folder("sent"))
        assert result is None

    @pytest.mark.asyncio
    async def test_skip_drafts_folder(self) -> None:
        """Returns None for drafts folder."""
        pipeline, _ = self._make_pipeline()
        result = await pipeline.process_message(_make_message(), _make_folder("drafts"))
        assert result is None

    @pytest.mark.asyncio
    async def test_not_spam_flow(self) -> None:
        """Not-spam verdict returns False, stores verdict, does not move."""
        pipeline, mocks = self._make_pipeline(verdict_is_spam=False)
        result = await pipeline.process_message(_make_message(), _make_folder())
        assert result is False
        mocks["verdict_repo"].create_verdict.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spam_flow_moves_to_spam(self) -> None:
        """Spam verdict returns True and triggers direct SQL spam move."""
        pipeline, mocks = self._make_pipeline(verdict_is_spam=True)

        # Mock the _move_to_spam to avoid DB access
        with patch.object(pipeline, "_move_to_spam", new_callable=AsyncMock) as mock_move:
            result = await pipeline.process_message(_make_message(), _make_folder())
            assert result is True
            mock_move.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embed_failure_returns_none(self) -> None:
        """Returns None when embedding fails."""
        pipeline, mocks = self._make_pipeline()
        mocks["store"].upsert = AsyncMock(return_value=False)
        result = await pipeline.process_message(_make_message(), _make_folder())
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_body_skipped(self) -> None:
        """Message with no embeddable text returns None."""
        pipeline, _ = self._make_pipeline()
        msg = _make_message(from_addr="", subject="", body_text="")
        result = await pipeline.process_message(msg, _make_folder())
        assert result is None
