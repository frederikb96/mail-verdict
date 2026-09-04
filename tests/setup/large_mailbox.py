"""
Bulk-seeds a mailbox directly into the messages mirror -- the fast,
opt-in alternative to tests/setup/mail_delivery.py's LMTP delivery for
anything that needs mailbox scale rather than mail content.

Delivering thousands of messages one at a time over LMTP is far too slow
to be usable in a test. This writes rows shaped exactly like what
PostIMAP's own sync engine would write once mail has actually synced --
the same explicit column list tests/pg/test_bulk_actions_and_outbox.py's
single-row _seed_messages() uses, scaled up via one bulk INSERT rather
than a loop.

A hand-built Table with only these columns, not the mapped Message class,
is deliberate: `insert(Message)` also silently pulls in every other
mapped column that carries a Python-side default (is_flagged,
is_answered, ...), which is harmless here but is exactly the "the ORM
writes columns the calling code never mentions" trap -- this way the
statement holds only the columns actually named below, auditable by
reading the INSERT it compiles to. Nothing here is reachable through a
grant a real consumer role holds; like every other pg-layer seed helper
in this suite, it runs against the migrated_db fixture's owner
connection, standing in for what PostIMAP itself would have written.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    MetaData,
    Table,
    Text,
    Uuid,
    insert,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

# The scale below which nothing in this suite needs bulk seeding --
# test_bulk_actions_and_outbox.py's plain per-row _seed_messages() is
# simpler and just as fast for a handful of messages.
LARGE_MAILBOX_MIN_COUNT = 1000


def _messages_insert_table() -> Table:
    """The exact column shape written below -- see the module docstring
    for why this is a bespoke Table rather than the mapped Message class."""
    return Table(
        "messages",
        MetaData(),
        Column("id", Uuid),
        Column("account_id", Uuid),
        Column("folder_id", Uuid),
        Column("imap_uid", BigInteger),
        Column("thread_id", Uuid),
        Column("message_id", Text),
        Column("subject", Text),
        Column("from_addr", Text),
        Column("received_at", DateTime(timezone=True)),
        Column("is_seen", Boolean),
    )


async def seed_large_mailbox_account(
    session: AsyncSession, *, imap_name: str = "INBOX",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a bare account with one folder, return (account_id, folder_id).

    Every pg test file seeds its own account inline rather than sharing one
    helper (see test_bulk_actions_and_outbox.py's _seed_account_two_folders
    for the two-folder version); this one only ever needs a single folder.
    """
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"large-mailbox-{account_id}"},
    )
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, :name)"),
        {"id": folder_id, "account_id": account_id, "name": imap_name},
    )
    return account_id, folder_id


async def seed_large_mailbox(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    count: int,
    *,
    uid_start: int = 1,
    unseen_every: int = 5,
) -> list[uuid.UUID]:
    """
    Bulk-insert `count` messages into one folder, oldest first by imap_uid.

    Every column filled in here is what a real synced message would carry
    -- subject, sender, a received_at spread one minute apart per message
    so date-grouping and received_at-ordered scroll positions have
    something real to sort. body_text/body_html/raw_source/raw_headers
    stay NULL: nothing that needs mailbox *scale* reads message bodies,
    and generating and storing thousands of them would only slow this
    down for no test that exists yet -- a test that needs a body on a
    message at scale should seed that one message individually alongside
    this call. One message in `unseen_every` is left unread, the rest
    marked seen; an all-read or all-unread folder exercises neither the
    unread count nor "mark all read" meaningfully.

    Measured against a fresh testcontainers Postgres+PostIMAP pair (one
    transaction, one INSERT -- SQLAlchemy's insertmanyvalues batches the
    parameter list under asyncpg's per-statement bind limit automatically,
    so this is a handful of round trips, not one per row): 1000 rows in
    ~1.5s, 14000 in ~25-30s. Bulk COPY measured the same order of
    magnitude for 14000 rows, because the cost is dominated by PostIMAP's
    own per-row AFTER triggers (folder counts, its event-notification
    channel) rather than by how the rows are sent -- real PostIMAP
    behaviour, not an artifact of this helper, and not something to work
    around here. A test seeding the full 14000 should raise its own
    pytest-timeout mark above the suite's 120s default rather than lean on
    it.

    Args:
        session: Active AsyncSession (caller commits)
        account_id: Owning account, from seed_large_mailbox_account or equivalent
        folder_id: Target folder
        count: Number of messages to insert (no upper bound enforced here)
        uid_start: First imap_uid -- lets a second call into the same
            folder avoid colliding with UNIQUE(folder_id, imap_uid)
        unseen_every: Every Nth message (by insertion/imap_uid order) is
            left unread

    Returns:
        The inserted message ids, in the same order as imap_uid
    """
    now = datetime.now(UTC)
    message_ids = [uuid.uuid4() for _ in range(count)]
    rows = [
        {
            "id": message_ids[i],
            "account_id": account_id,
            "folder_id": folder_id,
            "imap_uid": uid_start + i,
            "thread_id": uuid.uuid4(),
            "message_id": f"<{message_ids[i]}@large-mailbox.example.com>",
            "subject": f"Test message {uid_start + i}",
            "from_addr": f"sender{i % 50}@example.com",
            "received_at": now - timedelta(minutes=count - i),
            "is_seen": (i % unseen_every) != 0,
        }
        for i in range(count)
    ]
    await session.execute(insert(_messages_insert_table()), rows)
    return message_ids


async def build_large_mailbox(
    session: AsyncSession, count: int, *, imap_name: str = "INBOX",
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """seed_large_mailbox_account + seed_large_mailbox in one call -- the
    shape most callers actually want: a fresh account holding `count`
    messages in one folder, nothing else."""
    account_id, folder_id = await seed_large_mailbox_account(session, imap_name=imap_name)
    message_ids = await seed_large_mailbox(session, account_id, folder_id, count)
    return account_id, folder_id, message_ids
