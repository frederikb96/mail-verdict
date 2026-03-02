"""Tests for action executor: all 11 action types, template substitution, stop."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
    """Tests for ActionExecutor."""

    def _make_executor(
        self,
        propagator: MagicMock | None = None,
        tag_repo: MagicMock | None = None,
        notify_callback: AsyncMock | None = None,
    ) -> ActionExecutor:
        """Create an executor with mock dependencies."""
        prop = propagator or MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        prop.execute_forward = AsyncMock(return_value=True)
        prop.move_to_spam = AsyncMock(return_value=True)

        return ActionExecutor(
            propagator=prop,
            tag_repo=tag_repo,
            notify_callback=notify_callback,
        )

    @pytest.mark.asyncio
    async def test_move_to(self) -> None:
        """move_to action calls propagator.execute_imap."""
        prop = MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop)
        result = await executor.execute(
            {"move_to": "Archive"}, _ctx(folder="INBOX"), uid=1
        )
        assert result.success is True
        assert result.action_type == "move_to"

    @pytest.mark.asyncio
    async def test_copy_to(self) -> None:
        """copy_to action calls propagator.execute_imap."""
        prop = MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop)
        result = await executor.execute(
            {"copy_to": "Backup"}, _ctx(folder="INBOX"), uid=1
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_mark_as_read(self) -> None:
        """mark_as 'read' adds \\Seen flag."""
        prop = MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop)
        result = await executor.execute(
            {"mark_as": "read"}, _ctx(folder="INBOX"), uid=1
        )
        assert result.success is True
        prop.execute_imap.assert_awaited()

    @pytest.mark.asyncio
    async def test_mark_as_unread(self) -> None:
        """mark_as 'unread' removes \\Seen flag."""
        prop = MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop)
        result = await executor.execute(
            {"mark_as": "unread"}, _ctx(folder="INBOX"), uid=1
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_star(self) -> None:
        """star action adds \\Flagged flag."""
        prop = MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop)
        result = await executor.execute(
            {"star": True}, _ctx(folder="INBOX"), uid=1
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_unstar(self) -> None:
        """unstar action removes \\Flagged flag."""
        prop = MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop)
        result = await executor.execute(
            {"unstar": True}, _ctx(folder="INBOX"), uid=1
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_tag(self) -> None:
        """tag action adds tag in DB and IMAP."""
        tag_repo = MagicMock()
        tag_repo.add_tag = AsyncMock()
        prop = MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop, tag_repo=tag_repo)
        mail_id = uuid.uuid4()
        result = await executor.execute(
            {"tag": "billing"}, _ctx(folder="INBOX"), mail_id=mail_id, uid=1
        )
        assert result.success is True
        tag_repo.add_tag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_tag(self) -> None:
        """remove_tag action removes tag from DB and IMAP."""
        tag_repo = MagicMock()
        tag_repo.remove_tag = AsyncMock()
        prop = MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop, tag_repo=tag_repo)
        mail_id = uuid.uuid4()
        result = await executor.execute(
            {"remove_tag": "billing"}, _ctx(folder="INBOX"), mail_id=mail_id, uid=1
        )
        assert result.success is True
        tag_repo.remove_tag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trash(self) -> None:
        """trash action moves to Trash folder."""
        prop = MagicMock()
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop)
        result = await executor.execute(
            {"trash": True}, _ctx(folder="INBOX"), uid=1
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_forward_to_string(self) -> None:
        """forward_to with string address."""
        prop = MagicMock()
        prop.execute_forward = AsyncMock(return_value=True)
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop)
        result = await executor.execute(
            {"forward_to": "admin@example.com"}, _ctx(folder="INBOX", subject="Test"), uid=1
        )
        assert result.success is True
        prop.execute_forward.assert_awaited()

    @pytest.mark.asyncio
    async def test_forward_to_dict(self) -> None:
        """forward_to with dict config (address + subject_rewrite)."""
        prop = MagicMock()
        prop.execute_forward = AsyncMock(return_value=True)
        prop.execute_imap = AsyncMock(return_value=True)
        executor = self._make_executor(propagator=prop)
        result = await executor.execute(
            {"forward_to": {"address": "admin@example.com", "subject_rewrite": "Alert: {subject}"}},
            _ctx(folder="INBOX", subject="Test"),
            uid=1,
        )
        assert result.success is True

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
    async def test_no_propagator_logs_warning(self) -> None:
        """Actions requiring propagator are safe when propagator is None."""
        executor = ActionExecutor(propagator=None)
        result = await executor.execute({"move_to": "Archive"}, _ctx(), uid=1)
        assert result.success is True
