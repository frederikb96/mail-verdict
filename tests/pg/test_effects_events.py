"""
apply_effects() announcing what it just wrote -- a RecordVerdict that
actually recorded (not a duplicate the durability index absorbed) pushes
a verdict.issued event, the same one a user's own feedback pushes at
api/verdicts.py. Nothing else about apply_effects needs a real EventRing
in its tests today; this is the one effect whose write another viewer
needs to hear about.
"""

from __future__ import annotations

import uuid

import pytest

from mail_verdict.api.event_ring import EventRing
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.pipeline.context import FolderResolver
from mail_verdict.pipeline.contracts import RecordVerdict
from mail_verdict.pipeline.effects import apply_effects
from mail_verdict.pipeline.message_view import FolderView, MessageView


def _view(message_id: uuid.UUID, account_id: uuid.UUID) -> MessageView:
    return MessageView(
        message_id=message_id,
        msg_key=f"<{uuid.uuid4()}@example.com>",
        account_id=account_id,
        folder=FolderView(id=uuid.uuid4(), imap_name="INBOX", special_use=None),
        subject="hello",
        from_addr="sender@example.com",
        to_addrs=("me@example.com",),
        cc_addrs=(),
        headers={},
        body="body",
        body_truncated=False,
        size_bytes=4,
        received_at=None,
        is_seen=False,
        is_flagged=False,
        is_draft=False,
        is_truncated=False,
        keywords=(),
        tags=(),
        attachment_types=(),
        has_attachments=False,
        reply_to=None,
    )


@pytest.mark.asyncio
async def test_a_recorded_ai_verdict_announces_itself_over_the_event_stream(
    migrated_db: DatabaseConnection,
) -> None:
    message_id = uuid.uuid4()
    account_id = uuid.uuid4()
    view = _view(message_id, account_id)
    event_ring = EventRing()
    # replay_from(0, ...) reads as "gap too large" on a ring whose oldest
    # retained id is 1 -- ids start at 1, so 0 is indistinguishable from a
    # genuinely evicted id, and a fresh ring has never had anything to
    # evict. A harmless seed event gives the account a real oldest id to
    # measure a baseline against, the same way a real client's baseline is
    # always an id it actually received rather than a bare 0.
    await event_ring.add(account_id, "test.seed", {})
    seq_before = event_ring.get_latest_seq()

    _, applied = await apply_effects(
        migrated_db, view, (RecordVerdict(is_spam=True, reasoning="looks spammy"),),
        apply=True, folders=FolderResolver(migrated_db, account_id),
        event_ring=event_ring, stage_id="classify",
    )
    assert applied[0].applied is True

    new_events = await event_ring.replay_from(seq_before, str(account_id))
    matching = [e for e in new_events if e["event_type"] == "verdict.issued"]
    assert len(matching) == 1, f"expected exactly one verdict.issued event, got {new_events!r}"
    assert matching[0]["data"] == {
        "message_id": str(message_id), "is_spam": True,
        "source": "ai", "account_id": str(account_id),
    }


@pytest.mark.asyncio
async def test_a_duplicate_verdict_the_durability_index_absorbs_announces_nothing(
    migrated_db: DatabaseConnection,
) -> None:
    """The never-reclassify gate holds even against two runs racing on the
    same message (see _record_verdict's own docstring) -- the second
    apply_effects call here reports "not applied" for the same reason,
    and must not tell a viewer a verdict changed when nothing did."""
    message_id = uuid.uuid4()
    account_id = uuid.uuid4()
    view = _view(message_id, account_id)
    event_ring = EventRing()

    effect = RecordVerdict(is_spam=True, reasoning="looks spammy")
    await apply_effects(
        migrated_db, view, (effect,),
        apply=True, folders=FolderResolver(migrated_db, account_id),
        event_ring=event_ring, stage_id="classify",
    )
    seq_before = event_ring.get_latest_seq()

    _, applied = await apply_effects(
        migrated_db, view, (effect,),
        apply=True, folders=FolderResolver(migrated_db, account_id),
        event_ring=event_ring, stage_id="classify",
    )
    assert applied[0].applied is False

    new_events = await event_ring.replay_from(seq_before, str(account_id))
    assert new_events == []
