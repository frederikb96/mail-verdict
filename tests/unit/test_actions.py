"""Tests for action executor: template substitution, stop, unknown actions, tag/notify."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mail_verdict.rules.conditions import MailContext
from mail_verdict.rules.executor import (
    ActionExecutor,
    StopProcessing,
    _render_template,
)


def _ctx(**kwargs: Any) -> MailContext:
    """Create a MailContext with overrides."""
    return MailContext(**kwargs)


class TestRenderTemplate:
    """Tests for _render_template."""

    def test_from_substitution(self) -> None:
        ctx = _ctx(from_addr="alice@example.com")
        result = _render_template("From: {from}", ctx)
        assert result == "From: alice@example.com"

    def test_subject_substitution(self) -> None:
        ctx = _ctx(subject="Test Subject")
        result = _render_template("Fwd: {subject}", ctx)
        assert result == "Fwd: Test Subject"

    def test_multiple_vars(self) -> None:
        ctx = _ctx(from_addr="alice@example.com", subject="Hello", folder="INBOX")
        result = _render_template("{from} - {subject} ({folder})", ctx)
        assert "alice@example.com" in result
        assert "Hello" in result
        assert "INBOX" in result

    def test_missing_var_preserved(self) -> None:
        ctx = _ctx()
        result = _render_template("Hello {unknown_var}", ctx)
        assert "{unknown_var}" in result

    def test_tags_joined(self) -> None:
        ctx = _ctx(tags=["billing", "urgent"])
        result = _render_template("Tags: {tags}", ctx)
        assert "billing, urgent" in result


class TestActionExecutor:
    """Tests for ActionExecutor (PostIMAP mode, direct SQL UPDATEs)."""

    def _make_executor(
        self,
        tag_repo: MagicMock | None = None,
        notify_callback: AsyncMock | None = None,
        folder_repo: MagicMock | None = None,
    ) -> ActionExecutor:
        """Create an executor with mock dependencies."""
        return ActionExecutor(
            tag_repo=tag_repo,
            notify_callback=notify_callback,
            folder_repo=folder_repo,
        )

    @pytest.mark.asyncio
    async def test_tag(self) -> None:
        """tag action adds tag in DB (tag_repo call succeeds without DB)."""
        tag_repo = MagicMock()
        tag_repo.add_tag = AsyncMock()
        executor = self._make_executor(tag_repo=tag_repo)
        mail_id = uuid.uuid4()

        result = await executor.execute(
            {"tag": "billing"}, _ctx(folder="INBOX"), mail_id=mail_id, uid=1
        )
        assert result.success is True
        tag_repo.add_tag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_tag(self) -> None:
        """remove_tag action removes tag from DB."""
        tag_repo = MagicMock()
        tag_repo.remove_tag = AsyncMock()
        executor = self._make_executor(tag_repo=tag_repo)
        mail_id = uuid.uuid4()

        result = await executor.execute(
            {"remove_tag": "billing"}, _ctx(folder="INBOX"), mail_id=mail_id, uid=1
        )
        assert result.success is True
        tag_repo.remove_tag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notify(self) -> None:
        """notify action calls notify_callback."""
        cb = AsyncMock()
        executor = self._make_executor(notify_callback=cb)
        result = await executor.execute(
            {"notify": "New mail from {from}"}, _ctx(from_addr="alice@example.com"), uid=1
        )
        assert result.success is True
        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_raises(self) -> None:
        """stop action raises StopProcessing."""
        executor = self._make_executor()
        with pytest.raises(StopProcessing):
            await executor.execute({"stop": True}, _ctx(), uid=1)

    @pytest.mark.asyncio
    async def test_unknown_action(self) -> None:
        """Unknown action type returns failure."""
        executor = self._make_executor()
        result = await executor.execute({"unknown_action": "value"}, _ctx(), uid=1)
        assert result.success is False
        assert result.error == "Unknown action"

    @pytest.mark.asyncio
    async def test_move_to_no_mail_id(self) -> None:
        """move_to without mail_id logs warning and succeeds (no-op)."""
        executor = self._make_executor()
        result = await executor.execute({"move_to": "Archive"}, _ctx(), uid=1)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_move_to_with_mocks(self) -> None:
        """move_to with mail_id updates DB via direct SQL UPDATE."""
        account_id = uuid.uuid4()
        mail_id = uuid.uuid4()
        target_folder_id = uuid.uuid4()

        folder_repo = MagicMock()
        folder_repo.get_by_account = AsyncMock(return_value=[
            MagicMock(imap_name="Archive", id=target_folder_id),
            MagicMock(imap_name="INBOX", id=uuid.uuid4()),
        ])

        executor = self._make_executor(folder_repo=folder_repo)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()

        mock_db = MagicMock()
        mock_db.session.return_value = mock_session

        mock_resolve = AsyncMock(return_value=account_id)
        with (
            patch.object(executor, "_resolve_account_id", mock_resolve),
            patch(
                "mail_verdict.database.connection.get_db_connection",
                return_value=mock_db,
            ),
        ):
            result = await executor.execute(
                {"move_to": "Archive"}, _ctx(folder="INBOX"), mail_id=mail_id, uid=1
            )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_forward_to_logs_warning(self) -> None:
        """forward_to action is not yet supported."""
        executor = self._make_executor()
        result = await executor.execute(
            {"forward_to": "admin@example.com"}, _ctx(folder="INBOX", subject="Test"), uid=1
        )
        assert result.success is True
