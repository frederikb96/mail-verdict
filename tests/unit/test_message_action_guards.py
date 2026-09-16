"""
The parts of a guarded message action that no request through the endpoint
can reach: which folder each write is guarded to, and what makes two
requests the same request.
"""

from __future__ import annotations

import uuid

from pydantic import Field

from mail_verdict.api.mails import _write_guards
from mail_verdict.api.schemas import BulkActionRequest
from mail_verdict.mail_actions.submissions import request_fingerprint


class TestWriteGuards:
    def test_conversation_members_are_guarded_to_their_anchors_folder(self) -> None:
        """A member moved away between the read and the write must be left
        there as much as the anchor -- so its write is guarded too."""
        anchor, member, loose = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        folder, other = uuid.uuid4(), uuid.uuid4()
        guards = _write_guards(
            [anchor, loose],
            {anchor: folder, loose: other},
            {anchor: folder},
            [(anchor, folder), (member, folder), (loose, other)],
        )
        assert guards == {anchor: folder, member: folder, loose: None}

    def test_named_ids_are_guarded_only_when_the_caller_said_where(self) -> None:
        guarded, loose = uuid.uuid4(), uuid.uuid4()
        folder = uuid.uuid4()
        guards = _write_guards(
            [guarded, loose], {guarded: folder, loose: folder}, {guarded: folder}, None,
        )
        assert guards == {guarded: folder, loose: None}


class _BulkActionRequestLater(BulkActionRequest):
    """The same request body with a field a later release might add."""

    newer_option: bool = Field(default=False)


class TestFingerprint:
    def test_a_field_added_with_a_default_keeps_a_retry_the_same_request(self) -> None:
        """A client retrying across an upgrade that added an optional field
        sends the same request; refusing it as a different one would turn a
        safe retry into a failure."""
        account = uuid.uuid4()
        body = {"action": "archive", "ids": [str(uuid.uuid4())]}
        before = request_fingerprint("bulk", account, BulkActionRequest.model_validate(body))
        after = request_fingerprint("bulk", account, _BulkActionRequestLater.model_validate(body))
        assert before == after

    def test_a_different_request_has_a_different_fingerprint(self) -> None:
        account = uuid.uuid4()
        archive = BulkActionRequest.model_validate({"action": "archive", "ids": [str(account)]})
        trash = BulkActionRequest.model_validate({"action": "trash", "ids": [str(account)]})
        assert request_fingerprint("bulk", account, archive) != request_fingerprint(
            "bulk", account, trash,
        )
