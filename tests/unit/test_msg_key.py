"""
Unit tests for the durable message key (database/msg_key.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from mail_verdict.database.msg_key import compute_msg_key

_ACCOUNT_ID = uuid.uuid4()
_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_header_present_is_returned_verbatim() -> None:
    """When a Message-ID header exists, it is the key -- unchanged, angle brackets
    and all, so it stays the value every other lookup (PostIMAP's own storage,
    existing message_id_hdr rows) already compares against."""
    header = "<abc123@example.com>"
    key = compute_msg_key(
        account_id=_ACCOUNT_ID, message_id_hdr=header, from_addr="sender@example.com",
        subject="Hello", received_at=_RECEIVED_AT, size_bytes=1024,
    )
    assert key == header


def test_header_absent_falls_back_to_a_stable_hash() -> None:
    """No header produces a sha256: prefixed hash rather than an empty or null key --
    the whole point being that this message still gets a durable identity."""
    key = compute_msg_key(
        account_id=_ACCOUNT_ID, message_id_hdr=None, from_addr="sender@example.com",
        subject="Hello", received_at=_RECEIVED_AT, size_bytes=1024,
    )
    assert key.startswith("sha256:")
    assert len(key) == len("sha256:") + 64


def test_hash_is_stable_across_a_simulated_resync() -> None:
    """A UIDVALIDITY resync recreates the message row (and its id) without changing
    its envelope -- the hash fallback must be identical before and after, since it
    is what stands in for the row id no longer being trustworthy."""
    kwargs = dict(
        account_id=_ACCOUNT_ID, message_id_hdr=None, from_addr="sender@example.com",
        subject="Hello", received_at=_RECEIVED_AT, size_bytes=1024,
    )
    before = compute_msg_key(**kwargs)  # type: ignore[arg-type]
    after = compute_msg_key(**kwargs)  # type: ignore[arg-type]
    assert before == after


def test_hash_changes_when_the_sender_differs() -> None:
    """A different sender must not collide -- otherwise two distinct messages that
    happen to share subject/timestamp/size would be treated as one."""
    a = compute_msg_key(
        account_id=_ACCOUNT_ID, message_id_hdr=None, from_addr="alice@example.com",
        subject="Hello", received_at=_RECEIVED_AT, size_bytes=1024,
    )
    b = compute_msg_key(
        account_id=_ACCOUNT_ID, message_id_hdr=None, from_addr="bob@example.com",
        subject="Hello", received_at=_RECEIVED_AT, size_bytes=1024,
    )
    assert a != b


def test_hash_changes_when_account_differs() -> None:
    """The key is scoped per account -- two different accounts receiving an
    otherwise identical message must not collide on the same key."""
    a = compute_msg_key(
        account_id=uuid.uuid4(), message_id_hdr=None, from_addr="sender@example.com",
        subject="Hello", received_at=_RECEIVED_AT, size_bytes=1024,
    )
    b = compute_msg_key(
        account_id=uuid.uuid4(), message_id_hdr=None, from_addr="sender@example.com",
        subject="Hello", received_at=_RECEIVED_AT, size_bytes=1024,
    )
    assert a != b


def test_field_boundary_does_not_collide() -> None:
    """Concatenating fields without a separator would let ('ab', 'c') and
    ('a', 'bc') hash identically; the separator between fields is what prevents it."""
    a = compute_msg_key(
        account_id=_ACCOUNT_ID, message_id_hdr=None, from_addr="ab",
        subject="c", received_at=_RECEIVED_AT, size_bytes=1024,
    )
    b = compute_msg_key(
        account_id=_ACCOUNT_ID, message_id_hdr=None, from_addr="a",
        subject="bc", received_at=_RECEIVED_AT, size_bytes=1024,
    )
    assert a != b
