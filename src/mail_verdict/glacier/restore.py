"""
Restore: moving a glaciered message back onto the mail server (design
section 7). The only mechanism that can do this is an IMAP APPEND of
the stored bytes verbatim -- postimap/actions.py's insert_outbox_append,
gated on postimap.contract.supports_message_append() the same way every
other PostIMAP capability in this codebase is gated. Against an older
PostIMAP, restore answers unavailable rather than falling back to some
other mechanism: recomposing the message from structured fields (the way
an ordinary send/draft works) would lose the original Message-ID, the
DKIM signature and every received header, and there is no grant to
insert into `messages` directly.

Because restoring must work before removal is ever offered at all (this
feature's own overriding constraint), moving a message *into* the
glacier is gated on the same capability check -- see api/accounts.py's
own call to require_message_append_support.

The glacier copy is never deleted before the server's copy is observed:
between the APPEND syncing back and confirm_restore committing, the
message briefly exists in both the glacier and the target folder. That
window is short and self-clearing, and it is deliberately not hidden
earlier -- a failure during it would otherwise make the message
invisible everywhere.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import text

from mail_verdict.postimap.actions import insert_outbox_append
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RestoreOutcome:
    ok: bool
    reason: str | None
    outbox_id: uuid.UUID | None = None


async def require_message_append_support(db: DatabaseConnection) -> str | None:
    """
    The sequencing rule this feature's design states explicitly: restore
    must work before removal is ever offered at all, so moving a message
    *into* the glacier is refused on exactly the same capability check
    restoring it back out is.

    Args:
        db: Database connection

    Returns:
        None if supported, otherwise a message naming the running
        PostIMAP's service version for a 501 response
    """
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        version = info.service_version if info else "unknown"
        return (
            "the glacier needs a newer PostIMAP than this deployment runs "
            f"(service_version={version})"
        )
    return None


async def start_restore(
    db: DatabaseConnection, glacier_id: uuid.UUID, target_folder_id: uuid.UUID,
) -> RestoreOutcome:
    """
    Section 7 step 1: insert the APPEND and mark the row "restoring".
    `visible_at` is deliberately left set -- the glacier copy stays
    visible until confirm_restore proves the server's copy exists, so a
    failure in between never makes the message invisible everywhere.

    Only acts on a row already in "glaciered" -- restoring a message
    still mid-flight (copied/verified/removing, where the live row may
    still exist or an expunge is still in flight) is not handled here.

    Args:
        db: Database connection
        glacier_id: The glacier row to restore
        target_folder_id: Where to append it

    Returns:
        Whether the restore was started
    """
    capability_error = await require_message_append_support(db)
    if capability_error is not None:
        return RestoreOutcome(False, capability_error)

    async with db.session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT account_id, raw_source, is_seen, is_flagged, is_answered, keywords,
                           received_at, message_id
                    FROM glacier_messages WHERE id = :id AND state = 'glaciered'
                    """
                ),
                {"id": glacier_id},
            )
        ).mappings().one_or_none()
        if row is None:
            return RestoreOutcome(False, "message is not ready to restore")
        if row["raw_source"] is None:
            return RestoreOutcome(False, "this message has already been restored once")

        flags: list[str] = []
        if row["is_seen"]:
            flags.append("\\Seen")
        if row["is_flagged"]:
            flags.append("\\Flagged")
        if row["is_answered"]:
            flags.append("\\Answered")
        flags.extend(row["keywords"] or [])

        outbox = await insert_outbox_append(
            session, account_id=row["account_id"], raw_source=row["raw_source"],
            target_folder_id=target_folder_id, flags=flags,
            internal_date=row["received_at"] or datetime.now(timezone.utc),
        )
        await session.execute(
            text(
                "UPDATE glacier_messages SET state = 'restoring', restore_outbox_id = :oid, "
                "restore_started_at = now() WHERE id = :id"
            ),
            {"oid": outbox.id, "id": glacier_id},
        )
    return RestoreOutcome(True, None, outbox.id)


async def confirm_restores(
    db: DatabaseConnection, account_id: uuid.UUID, *, event_ring: EventRing | None = None,
    batch_size: int = 50,
) -> int:
    """
    Section 7 step 3-4: for each row in "restoring", look for the
    confirming live row -- same account, its target folder, the same
    Message-ID header, `imap_uid IS NOT NULL` (genuinely on the server,
    not another pending move), matching size. Found: re-point
    tags/verdicts/embedding hint, set restored_at, clear visible_at, and
    null the bulk columns (D9 -- the tombstone shape) in one transaction,
    then announce the move.

    Args:
        db: Database connection
        account_id: Account to check
        event_ring: Optional SSE ring
        batch_size: How many restoring rows to consider in one tick

    Returns:
        How many were confirmed
    """
    confirmed = 0
    async with db.session() as session:
        restoring = (
            await session.execute(
                text(
                    """
                    SELECT g.id, g.message_id, g.size_bytes, g.restore_outbox_id
                    FROM glacier_messages g
                    JOIN outbox o ON o.id = g.restore_outbox_id
                    WHERE g.account_id = :account_id AND g.state = 'restoring'
                    LIMIT :batch
                    """
                ),
                {"account_id": account_id, "batch": batch_size},
            )
        ).mappings().all()
        for row in restoring:
            outbox_row = (
                await session.execute(
                    text(
                        "SELECT status, target_folder_id FROM outbox WHERE id = :id"
                    ),
                    {"id": row["restore_outbox_id"]},
                )
            ).mappings().one_or_none()
            if outbox_row is None:
                continue
            live = (
                await session.execute(
                    text(
                        """
                        SELECT id, folder_id FROM messages
                        WHERE account_id = :account_id AND expunged_at IS NULL
                          AND imap_uid IS NOT NULL
                          AND coalesce(message_id, '') = coalesce(:message_id_hdr, '')
                          AND size_bytes IS NOT DISTINCT FROM :size_bytes
                          AND folder_id = :target_folder_id
                        LIMIT 1
                        """
                    ),
                    {
                        "account_id": account_id, "message_id_hdr": row["message_id"],
                        "size_bytes": row["size_bytes"],
                        "target_folder_id": outbox_row["target_folder_id"],
                    },
                )
            ).mappings().one_or_none()
            if live is None:
                continue

            await session.execute(
                text("UPDATE mail_tags SET mail_id = :new_id WHERE mail_id = :gid"),
                {"new_id": live["id"], "gid": row["id"]},
            )
            await session.execute(
                text("UPDATE verdicts SET mail_id = :new_id WHERE mail_id = :gid"),
                {"new_id": live["id"], "gid": row["id"]},
            )
            await session.execute(
                text(
                    "UPDATE message_embeddings SET message_id = :new_id "
                    "WHERE message_id IS NULL AND account_id = :account_id AND msg_key = "
                    "(SELECT msg_key FROM glacier_messages WHERE id = :gid)"
                ),
                {"new_id": live["id"], "account_id": account_id, "gid": row["id"]},
            )
            await session.execute(
                text("DELETE FROM glacier_attachments WHERE glacier_message_id = :gid"),
                {"gid": row["id"]},
            )
            await session.execute(
                text(
                    """
                    UPDATE glacier_messages
                    SET restored_at = now(), visible_at = NULL, raw_source = NULL,
                        body_text = NULL, body_html = NULL, raw_headers = NULL
                    WHERE id = :gid
                    """
                ),
                {"gid": row["id"]},
            )
            confirmed += 1
            if event_ring is not None:
                await event_ring.add(
                    account_id, "mail.updated",
                    {
                        "id": str(live["id"]), "account_id": str(account_id),
                        "folder_id": str(live["folder_id"]), "old_folder_id": str(row["id"]),
                        "changed": ["folder_id"],
                    },
                )
                await event_ring.add(
                    account_id, "folder.changed", {"account_id": str(account_id)},
                )
    return confirmed


async def fail_stale_restores(
    db: DatabaseConnection, account_id: uuid.UUID, *, timeout_seconds: int,
) -> int:
    """
    Section 7 step 5: a restore whose outbox row dead-lettered, or that
    has simply run past its timeout with no live row appearing, goes
    back to "glaciered" for a retry -- the glacier copy was never
    deleted, so nothing here is destructive.

    Args:
        db: Database connection
        account_id: Account to check
        timeout_seconds: How long a restore may run before it counts as
            failed

    Returns:
        How many were reverted
    """
    failed = 0
    async with db.session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT g.id, o.status
                    FROM glacier_messages g
                    LEFT JOIN outbox o ON o.id = g.restore_outbox_id
                    WHERE g.account_id = :account_id AND g.state = 'restoring'
                      AND (
                        o.status = 'dead'
                        OR g.restore_started_at < now() - make_interval(secs => :timeout)
                      )
                    """
                ),
                {"account_id": account_id, "timeout": timeout_seconds},
            )
        ).mappings().all()
        for row in rows:
            reason = "the restore's outbox entry failed permanently" if row["status"] == "dead" \
                else "the restore did not complete within the timeout"
            await session.execute(
                text(
                    "UPDATE glacier_messages SET state = 'glaciered', last_error = :reason "
                    "WHERE id = :gid"
                ),
                {"reason": reason, "gid": row["id"]},
            )
            failed += 1
            logger.warning(
                "Glacier restore failed, copy intact",
                extra={"account_id": str(account_id), "glacier_id": str(row["id"])},
            )
    return failed
