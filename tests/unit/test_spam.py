"""
Unit tests for spam detection pipeline.

Row 151 (o=10.43): VerdictPipeline flow, malformed response handling, disabled spam.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.database.models import SpecialUse
from mail_verdict.spam.analyst import (
    AnalysisContext,
    NeighborContext,
    SpamVerdict,
    _auth_str,
    _build_user_prompt,
    _parse_verdict,
)
from mail_verdict.spam.pipeline import VerdictPipeline

pytestmark = pytest.mark.unit


# ===========================================================================
# SpamAnalyst helper tests
# ===========================================================================


class TestAuthStr:
    """Tests for _auth_str helper."""

    def test_pass(self) -> None:
        assert _auth_str(True) == "pass"

    def test_fail(self) -> None:
        assert _auth_str(False) == "fail"

    def test_unknown(self) -> None:
        assert _auth_str(None) == "unknown"


class TestParseVerdict:
    """Tests for _parse_verdict parsing logic."""

    def test_valid_spam(self) -> None:
        """Parse valid spam verdict."""
        result = _parse_verdict('{"verdict": "spam"}')
        assert result.is_spam is True

    def test_valid_not_spam(self) -> None:
        """Parse valid not-spam verdict."""
        result = _parse_verdict('{"verdict": "not-spam"}')
        assert result.is_spam is False

    def test_invalid_json(self) -> None:
        """Raises ValueError on invalid JSON."""
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_verdict("not json at all")

    def test_invalid_verdict_value(self) -> None:
        """Raises ValueError on unexpected verdict string."""
        with pytest.raises(ValueError, match="Invalid verdict"):
            _parse_verdict('{"verdict": "maybe"}')

    def test_missing_verdict_key(self) -> None:
        """Raises ValueError when verdict key is absent."""
        with pytest.raises(ValueError, match="Invalid verdict"):
            _parse_verdict('{"result": "spam"}')


class TestAnalysisContext:
    """Tests for AnalysisContext serialization."""

    def test_to_dict_full(self) -> None:
        """Serialization includes all fields."""
        ctx = AnalysisContext(
            mail_id="test-id",
            from_addr="sender@test.com",
            to_addrs="recipient@test.com",
            subject="Test Subject",
            body_excerpt="Test body",
            dkim_pass=True,
            spf_pass=False,
            dmarc_pass=None,
            neighbors=[
                NeighborContext(mail_id="n1", tag="spam", excerpt="bad stuff"),
            ],
        )
        result = ctx.to_dict()
        assert result["new_mail"]["from"] == "sender@test.com"
        assert result["new_mail"]["auth"]["dkim"] == "pass"
        assert result["new_mail"]["auth"]["spf"] == "fail"
        assert result["new_mail"]["auth"]["dmarc"] == "unknown"
        assert len(result["neighbors"]) == 1

    def test_to_dict_empty(self) -> None:
        """Serialization handles None fields."""
        ctx = AnalysisContext(
            mail_id="test-id",
            from_addr=None,
            to_addrs=None,
            subject=None,
            body_excerpt="",
        )
        result = ctx.to_dict()
        assert result["new_mail"]["from"] == ""
        assert result["new_mail"]["subject"] == ""


class TestBuildUserPrompt:
    """Tests for _build_user_prompt."""

    def test_contains_context(self) -> None:
        """User prompt contains serialized context."""
        ctx = AnalysisContext(
            mail_id="test",
            from_addr="a@b.com",
            to_addrs="c@d.com",
            subject="Hello",
            body_excerpt="World",
        )
        prompt = _build_user_prompt(ctx)
        assert "a@b.com" in prompt
        assert "Hello" in prompt
        assert "World" in prompt


# ===========================================================================
# VerdictPipeline tests
# ===========================================================================


class TestVerdictPipeline:
    """Tests for the spam verdict pipeline orchestration."""

    @pytest.fixture
    def mock_mail(self) -> MagicMock:
        """Mock Mail ORM object."""
        mail = MagicMock()
        mail.id = uuid.UUID("00000000-0000-0000-0000-000000000003")
        mail.account_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mail.from_addr = "sender@example.com"
        mail.subject = "Test Email"
        mail.body_text = "This is a test email body for spam analysis."
        mail.to_addrs = {"addrs": ["recipient@example.com"]}
        mail.uid = 42
        mail.dkim_pass = True
        mail.spf_pass = True
        mail.dmarc_pass = True
        mail.received_at = datetime.now(timezone.utc)
        return mail

    @pytest.fixture
    def mock_folder(self) -> MagicMock:
        """Mock Folder ORM object for INBOX."""
        folder = MagicMock()
        folder.special_use = SpecialUse.INBOX
        folder.imap_name = "INBOX"
        return folder

    @pytest.fixture
    def mock_analyst(self) -> AsyncMock:
        """Mock SpamAnalyst."""
        analyst = AsyncMock()
        analyst.analyze = AsyncMock(
            return_value=SpamVerdict(is_spam=False, raw_response={"verdict": "not-spam"})
        )
        return analyst

    @pytest.fixture
    def mock_verdict_repo(self) -> AsyncMock:
        """Mock VerdictRepository."""
        repo = AsyncMock()
        repo.create_verdict = AsyncMock()
        return repo

    @pytest.fixture
    def mock_mail_repo(self) -> AsyncMock:
        """Mock MailRepository."""
        repo = AsyncMock()
        return repo

    @pytest.fixture
    def pipeline(
        self,
        test_config: MagicMock,
        mock_semantic_store: AsyncMock,
        mock_analyst: AsyncMock,
        mock_verdict_repo: AsyncMock,
        mock_mail_repo: AsyncMock,
        mock_action_propagator: AsyncMock,
    ) -> VerdictPipeline:
        """Create a VerdictPipeline with all dependencies mocked."""
        return VerdictPipeline(
            config=test_config,
            semantic_store=mock_semantic_store,
            analyst=mock_analyst,
            verdict_repo=mock_verdict_repo,
            mail_repo=mock_mail_repo,
            action_propagator=mock_action_propagator,
        )

    @pytest.mark.asyncio(loop_scope="function")
    async def test_process_not_spam(
        self,
        pipeline: VerdictPipeline,
        mock_mail: MagicMock,
        mock_folder: MagicMock,
    ) -> None:
        """Pipeline returns False for not-spam verdict."""
        result = await pipeline.process_mail(mock_mail, mock_folder)
        assert result is False

    @pytest.mark.asyncio(loop_scope="function")
    async def test_process_spam(
        self,
        pipeline: VerdictPipeline,
        mock_mail: MagicMock,
        mock_folder: MagicMock,
        mock_analyst: AsyncMock,
    ) -> None:
        """Pipeline returns True for spam verdict and triggers move."""
        mock_analyst.analyze.return_value = SpamVerdict(
            is_spam=True,
            raw_response={"verdict": "spam"},
        )
        result = await pipeline.process_mail(mock_mail, mock_folder)
        assert result is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_skip_when_disabled(
        self,
        disabled_spam_config: MagicMock,
        mock_semantic_store: AsyncMock,
        mock_mail: MagicMock,
        mock_folder: MagicMock,
    ) -> None:
        """Pipeline returns None when spam detection is disabled."""
        pipeline = VerdictPipeline(
            config=disabled_spam_config,
            semantic_store=mock_semantic_store,
            analyst=AsyncMock(),
            verdict_repo=AsyncMock(),
            mail_repo=AsyncMock(),
        )
        result = await pipeline.process_mail(mock_mail, mock_folder)
        assert result is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_skip_sent_folder(
        self,
        pipeline: VerdictPipeline,
        mock_mail: MagicMock,
    ) -> None:
        """Pipeline skips Sent folder."""
        folder = MagicMock()
        folder.special_use = SpecialUse.SENT
        folder.imap_name = "Sent"

        result = await pipeline.process_mail(mock_mail, folder)
        assert result is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_skip_trash_folder(
        self,
        pipeline: VerdictPipeline,
        mock_mail: MagicMock,
    ) -> None:
        """Pipeline skips Trash folder."""
        folder = MagicMock()
        folder.special_use = SpecialUse.TRASH
        folder.imap_name = "Trash"

        result = await pipeline.process_mail(mock_mail, folder)
        assert result is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_embed_failure_returns_none(
        self,
        pipeline: VerdictPipeline,
        mock_mail: MagicMock,
        mock_folder: MagicMock,
        mock_semantic_store: AsyncMock,
    ) -> None:
        """Pipeline returns None when embedding fails."""
        mock_semantic_store.upsert.return_value = False
        result = await pipeline.process_mail(mock_mail, mock_folder)
        assert result is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_empty_body_returns_none(
        self,
        pipeline: VerdictPipeline,
        mock_mail: MagicMock,
        mock_folder: MagicMock,
    ) -> None:
        """Pipeline returns None for mails with empty body."""
        mock_mail.from_addr = None
        mock_mail.subject = None
        mock_mail.body_text = ""
        result = await pipeline.process_mail(mock_mail, mock_folder)
        assert result is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_exception_returns_none(
        self,
        pipeline: VerdictPipeline,
        mock_mail: MagicMock,
        mock_folder: MagicMock,
        mock_semantic_store: AsyncMock,
    ) -> None:
        """Pipeline catches exceptions and returns None."""
        mock_semantic_store.upsert.side_effect = RuntimeError("Qdrant down")
        result = await pipeline.process_mail(mock_mail, mock_folder)
        assert result is None
