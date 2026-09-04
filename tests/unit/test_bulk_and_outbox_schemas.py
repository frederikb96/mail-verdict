"""Tests for the bulk-action and outbox request schemas."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError


class TestBulkActionRequestExclusivity:
    """Tests for the ids-and/or-scope selection mechanism."""

    def test_rejects_neither_ids_nor_scope(self) -> None:
        """A request naming no selection at all is refused, not treated as empty."""
        from mail_verdict.api.schemas import BulkActionRequest

        with pytest.raises(ValidationError):
            BulkActionRequest(action="mark_read")

    def test_accepts_both_ids_and_scope(self) -> None:
        """A predicate scope plus explicit ids on top of it is meaningful --
        e.g. everything the scope matched, plus a row outside it the user
        ticked by hand -- so both together validates."""
        from mail_verdict.api.schemas import BulkActionRequest, BulkActionScope

        extra_id = uuid.uuid4()
        request = BulkActionRequest(
            action="mark_read",
            ids=[extra_id],
            scope=BulkActionScope(
                folder_id=uuid.uuid4(), snapshot_at=datetime.now(UTC),
            ),
        )
        assert request.ids == [extra_id]
        assert request.scope is not None

    def test_rejects_an_id_named_in_both_ids_and_exclude_ids(self) -> None:
        """An id both explicitly included and explicitly excluded is a
        client bug -- refused rather than picking a winner silently."""
        from mail_verdict.api.schemas import BulkActionRequest, BulkActionScope

        contradictory_id = uuid.uuid4()
        with pytest.raises(ValidationError):
            BulkActionRequest(
                action="mark_read",
                ids=[contradictory_id],
                scope=BulkActionScope(
                    folder_id=uuid.uuid4(),
                    snapshot_at=datetime.now(UTC),
                    exclude_ids=[contradictory_id],
                ),
            )

    def test_accepts_ids_only(self) -> None:
        """A request naming only an id list validates."""
        from mail_verdict.api.schemas import BulkActionRequest

        ids = [uuid.uuid4(), uuid.uuid4()]
        request = BulkActionRequest(action="trash", ids=ids)
        assert request.ids == ids
        assert request.scope is None

    def test_accepts_scope_only(self) -> None:
        """A request naming only a scope validates."""
        from mail_verdict.api.schemas import BulkActionRequest, BulkActionScope

        scope = BulkActionScope(
            folder_id=uuid.uuid4(), filter="unread", snapshot_at=datetime.now(UTC),
        )
        request = BulkActionRequest(action="flag", scope=scope)
        assert request.ids is None
        assert request.scope is scope

    def test_scope_requires_snapshot_at(self) -> None:
        """A missing snapshot_at would silently mean 'everything, including
        whatever arrived since' -- refused rather than defaulted."""
        from mail_verdict.api.schemas import BulkActionScope

        with pytest.raises(ValidationError):
            BulkActionScope(folder_id=uuid.uuid4())

    def test_move_without_target_folder_id_still_validates(self) -> None:
        """target_folder_id is optional at the schema level -- callers enforce it per-action."""
        from mail_verdict.api.schemas import BulkActionRequest

        request = BulkActionRequest(action="move", ids=[uuid.uuid4()])
        assert request.target_folder_id is None


class TestOutboxCreateRequest:
    """Tests for the outbox send/draft request schema."""

    def test_rejects_unknown_kind(self) -> None:
        """kind is restricted to send/draft -- an arbitrary string is refused."""
        from mail_verdict.api.schemas import OutboxCreateRequest

        with pytest.raises(ValidationError):
            OutboxCreateRequest(
                account_id=uuid.uuid4(), kind="queued", to=["a@example.com"],
            )

    def test_accepts_draft_without_recipients(self) -> None:
        """A draft may have no recipients yet -- to defaults to empty, not required."""
        from mail_verdict.api.schemas import OutboxCreateRequest

        request = OutboxCreateRequest(account_id=uuid.uuid4(), kind="draft")
        assert request.to == []


class TestMessageActionRequestActions:
    """Tests for the message action verb set."""

    def test_rejects_the_old_unified_delete_verb(self) -> None:
        """'delete' was replaced by the explicit trash/expunge split -- it must not validate."""
        from mail_verdict.api.schemas import MessageActionRequest

        with pytest.raises(ValidationError):
            MessageActionRequest(action="delete")

    def test_trash_and_expunge_are_distinct_verbs(self) -> None:
        """Both the reversible and permanent removal actions validate independently."""
        from mail_verdict.api.schemas import MessageActionRequest

        assert MessageActionRequest(action="trash").action == "trash"
        assert MessageActionRequest(action="expunge").action == "expunge"

    def test_keyword_actions_carry_a_keyword_value(self) -> None:
        """keyword_add/keyword_remove accept the keyword field the other verbs ignore."""
        from mail_verdict.api.schemas import MessageActionRequest

        request = MessageActionRequest(action="keyword_add", keyword="important")
        assert request.keyword == "important"
