"""Tests for VerdictPipeline: gating logic and the spam/not-spam flow (mocked)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mail_verdict.spam.analyst import SpamVerdict
from mail_verdict.spam.pipeline import _SKIP_FOLDER_SPECIAL_USE, VerdictPipeline


def _make_settings_service(enabled: bool = True) -> MagicMock:
    """Create a mock SettingsService."""
    service = MagicMock()
    service.get = MagicMock(side_effect=lambda cat: {
        "spam": {
            "enabled": enabled,
            "excerpt_length": 300,
            "auto_move_to_junk": True,
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
    is_draft: bool = False,
    message_id: str = "<m-1@example.com>",
) -> MagicMock:
    """Create a mock Message object."""
    msg = MagicMock()
    msg.id = uuid.uuid4()
    msg.account_id = _TEST_ACCOUNT_ID
    msg.imap_uid = 42
    msg.message_id = message_id
    msg.from_addr = from_addr
    msg.subject = subject
    msg.body_text = body_text
    msg.is_draft = is_draft
    msg.is_truncated = False
    msg.to_addrs = ["bob@example.com"]
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


class TestSkipFolderSpecialUse:
    """Tests for the folder-type-based skip set."""

    def test_skip_sent(self) -> None:
        assert "sent" in _SKIP_FOLDER_SPECIAL_USE

    def test_skip_drafts(self) -> None:
        assert "drafts" in _SKIP_FOLDER_SPECIAL_USE

    def test_skip_trash(self) -> None:
        assert "trash" in _SKIP_FOLDER_SPECIAL_USE

    def test_skip_junk(self) -> None:
        assert "junk" in _SKIP_FOLDER_SPECIAL_USE


class TestVerdictPipeline:
    """Tests for VerdictPipeline.process_message (contract SQL, no embeddings)."""

    def _make_pipeline(
        self,
        settings_service: MagicMock | None = None,
        verdict_is_spam: bool = False,
        existing_ai_verdict: bool = False,
        account_spam_enabled: bool | None = True,
    ) -> tuple[VerdictPipeline, dict[str, MagicMock]]:
        """
        Create pipeline with mock dependencies, return (pipeline, mocks).

        account_spam_enabled=None simulates no account_prefs row at all
        (get_by_account returns None); the gate must fail closed on that,
        the same as an explicit False.
        """
        settings_service = settings_service or _make_settings_service()

        analyst = MagicMock()
        analyst.analyze = AsyncMock(
            return_value=SpamVerdict(
                is_spam=verdict_is_spam,
                raw_response={"verdict": "spam" if verdict_is_spam else "not-spam"},
            )
        )

        verdict_repo = MagicMock()
        verdict_repo.create_verdict = AsyncMock()
        verdict_repo.has_ai_verdict_for_header = AsyncMock(return_value=existing_ai_verdict)

        folder_repo = MagicMock()
        folder_repo.get_by_account = AsyncMock(return_value=[])
        # Overridden per-test (via mocks["folder_repo"].get_effective_special_use)
        # to match whatever special_use the test's _make_folder() carries.
        folder_repo.get_effective_special_use = AsyncMock(return_value=None)

        account_prefs_repo = MagicMock()
        prefs = None
        if account_spam_enabled is not None:
            prefs = MagicMock(spam_enabled=account_spam_enabled)
        account_prefs_repo.get_by_account = AsyncMock(return_value=prefs)

        db = MagicMock()

        pipeline = VerdictPipeline(
            settings_service=settings_service,
            analyst=analyst,
            verdict_repo=verdict_repo,
            folder_repo=folder_repo,
            account_prefs_repo=account_prefs_repo,
            db=db,
        )

        mocks = {
            "analyst": analyst,
            "verdict_repo": verdict_repo,
            "folder_repo": folder_repo,
            "account_prefs_repo": account_prefs_repo,
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
        pipeline, mocks = self._make_pipeline()
        mocks["folder_repo"].get_effective_special_use.return_value = "sent"
        result = await pipeline.process_message(_make_message(), _make_folder("sent"))
        assert result is None
        mocks["analyst"].analyze.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skip_drafts_folder(self) -> None:
        """Returns None for drafts folder."""
        pipeline, mocks = self._make_pipeline()
        mocks["folder_repo"].get_effective_special_use.return_value = "drafts"
        result = await pipeline.process_message(_make_message(), _make_folder("drafts"))
        assert result is None

    @pytest.mark.asyncio
    async def test_skip_folder_override_to_junk(self) -> None:
        """
        A folder whose raw special_use is unset but has a folder_prefs override
        is still gated -- the pipeline must read the effective value, not
        Folder.special_use directly.
        """
        pipeline, mocks = self._make_pipeline()
        mocks["folder_repo"].get_effective_special_use.return_value = "junk"
        result = await pipeline.process_message(_make_message(), _make_folder(None))
        assert result is None
        mocks["analyst"].analyze.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skip_draft_flag(self) -> None:
        """Returns None for a draft message regardless of folder."""
        pipeline, _ = self._make_pipeline()
        result = await pipeline.process_message(_make_message(is_draft=True), _make_folder())
        assert result is None

    @pytest.mark.asyncio
    async def test_durability_gate_skips_already_classified(self) -> None:
        """A message whose header already has an AI verdict is never reclassified."""
        pipeline, mocks = self._make_pipeline(existing_ai_verdict=True)
        result = await pipeline.process_message(_make_message(), _make_folder())
        assert result is None
        mocks["analyst"].analyze.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_spam_flow(self) -> None:
        """Not-spam verdict returns False and stores a verdict row."""
        pipeline, mocks = self._make_pipeline(verdict_is_spam=False)
        result = await pipeline.process_message(_make_message(), _make_folder())
        assert result is False
        mocks["verdict_repo"].create_verdict.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spam_flow_moves_to_junk(self) -> None:
        """Spam verdict returns True and triggers a move to the junk folder."""
        pipeline, mocks = self._make_pipeline(verdict_is_spam=True)

        with patch.object(pipeline, "_move_to_junk", new_callable=AsyncMock) as mock_move:
            result = await pipeline.process_message(_make_message(), _make_folder())
            assert result is True
            mock_move.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_account_spam_disabled_returns_none(self) -> None:
        """
        The per-account toggle gates classification independently of the
        global setting -- spam enabled globally must not classify an
        account that has explicitly turned it off.
        """
        pipeline, mocks = self._make_pipeline(account_spam_enabled=False)
        result = await pipeline.process_message(_make_message(), _make_folder())
        assert result is None
        mocks["analyst"].analyze.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_account_prefs_fails_closed(self) -> None:
        """No account_prefs row at all must not be treated as opted in."""
        pipeline, mocks = self._make_pipeline(account_spam_enabled=None)
        result = await pipeline.process_message(_make_message(), _make_folder())
        assert result is None
        mocks["analyst"].analyze.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enabling_spam_through_settings_takes_effect_without_rebuild(self) -> None:
        """
        Flipping spam.enabled on the same settings cache changes the
        pipeline's next call, with no new VerdictPipeline constructed.

        Regression test: spam.enabled used to only be read once, at
        server startup, to decide whether to build the pipeline at all.
        """
        settings_state = {"enabled": False}
        settings_service = MagicMock()
        settings_service.get = MagicMock(side_effect=lambda cat: {
            "spam": {
                "enabled": settings_state["enabled"],
                "excerpt_length": 300,
                "auto_move_to_junk": True,
                "auto_mark_read": True,
            },
            "ai": {"model": "test-model"},
        }.get(cat, {}))

        pipeline, mocks = self._make_pipeline(settings_service=settings_service)

        result = await pipeline.process_message(_make_message(), _make_folder())
        assert result is None
        mocks["analyst"].analyze.assert_not_awaited()

        settings_state["enabled"] = True

        result = await pipeline.process_message(_make_message(), _make_folder())
        assert result is False
        mocks["analyst"].analyze.assert_awaited_once()
