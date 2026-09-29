"""
The glacier's write sequence: copy, verify, expunge, and the bookkeeping
that confirms or withdraws an expunge and repairs what a UIDVALIDITY
resync or a split conversation leaves behind.

Every step below commits as its own transaction and is written to be
safely re-run: a step that already ran for a message is a no-op, and a
step that never got to run leaves the message exactly where the
previous one left it. That is what makes a crash between any two steps
recoverable by the next sweep tick rather than by a human -- see the
module-level table in this feature's design for the full failure-mode
enumeration this follows.

The copy, the hash and the verify never load message bytes into Python:
every statement below that touches `raw_source` or attachment `data`
does so entirely in SQL. Hashing hundreds of megabytes inside an
`async def` would block the event loop for every other request in the
process for as long as it ran -- see the repo's own note on exactly
this failure shape.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.glacier.restore import require_message_append_support
from mail_verdict.postimap.actions import expunge_if_matches

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

CopyStatus = Literal[
    "copied", "refilled", "duplicate_removable", "duplicate_conflict", "ineligible",
]
ExpungeResult = Literal["expunged", "rolled_back", "not_ready"]

# Every guard a message must clear before it may enter the glacier at
# all -- checked in the same statement that copies, and re-checked by
# eligibility_reason() individually so a refusal can say which one
# failed. `a`/`f`/`m` are the accounts/folders/messages aliases every
# caller below joins in the same order.
#
# Deliberately `a.is_active = true` alone, not also `a.state = 'active'`.
# `state = 'error'` means retrying, not dead -- PostIMAP keeps retrying
# unboundedly with backoff, and the mirror data underneath it stays
# valid the whole time; only `disabled` (is_active = false) means it has
# stopped. Requiring 'active' specifically would refuse every message on
# an account having a bad few minutes for no data-safety reason: the
# expunge this copy leads to is a queued write like any other and simply
# waits for the account to reconnect, the same as a flag change or a
# send would.
_ELIGIBILITY_SQL = """
    m.expunged_at IS NULL
    AND m.is_truncated = false
    AND m.raw_source IS NOT NULL
    AND m.imap_uid IS NOT NULL
    AND m.is_draft = false
    AND f.initial_sync_done = true
    AND f.deleted_at IS NULL
    AND a.is_active = true
"""

_GLACIER_ATTACHMENT_COPY_SQL = """
    INSERT INTO glacier_attachments (
        id, glacier_message_id, source_attachment_id, filename,
        content_type, content_id, size_bytes, data
    )
    SELECT gen_random_uuid(), :gid, a.id, a.filename, a.content_type,
           a.content_id, a.size_bytes, a.data
    FROM attachments a WHERE a.message_id = :origin_id
"""


@dataclass(frozen=True)
class CopyOutcome:
    """What copy_message did. `glacier_id` is set for every status except
    "ineligible"; for "duplicate_removable"/"duplicate_conflict" it names
    the *existing* row that already claims this message's identity, not
    a new one."""

    status: CopyStatus
    glacier_id: uuid.UUID | None
    account_id: uuid.UUID | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ManualOutcome:
    """The result of a synchronous, single-message glacier-now call (the
    manual move action)."""

    ok: bool
    reason: str | None
    glacier_id: uuid.UUID | None = None
    pending: bool = False


async def eligibility_reason(session: AsyncSession, message_id: uuid.UUID) -> str | None:
    """
    Why a message cannot enter the glacier right now, or None if it can.

    Checked as individual guards rather than as the absence of one query's
    result, so a caller -- an API refusal, a log line -- can say which
    guard failed instead of just "no".

    Args:
        session: Active AsyncSession
        message_id: The message to check

    Returns:
        A short reason, or None if the message is eligible
    """
    row = (
        await session.execute(
            text(
                """
                SELECT m.expunged_at, m.is_truncated, m.raw_source IS NOT NULL AS has_raw,
                       m.imap_uid, m.is_draft, f.initial_sync_done, f.deleted_at,
                       a.is_active, a.state
                FROM messages m
                JOIN folders f ON f.id = m.folder_id
                JOIN accounts a ON a.id = m.account_id
                WHERE m.id = :id
                """
            ),
            {"id": message_id},
        )
    ).mappings().one_or_none()
    if row is None:
        return "message not found"
    if row["expunged_at"] is not None:
        return "message already expunged"
    if row["is_truncated"] or not row["has_raw"]:
        return "message body was never fully fetched"
    if row["imap_uid"] is None:
        return "a move on this message is still pending"
    if row["is_draft"]:
        return "drafts cannot be glaciered"
    if not row["initial_sync_done"] or row["deleted_at"] is not None:
        return "this folder has not finished its first sync"
    if not row["is_active"]:
        return "the account is disabled"
    return None


async def _handle_existing_glacier_row(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    msg_key: str,
    message_id: uuid.UUID,
) -> CopyOutcome:
    """
    Section 3.7: what an INSERT ... ON CONFLICT that returned no row
    means. Either a non-tombstone row already claims (account_id,
    msg_key) -- compare content and decide duplicate_removable vs
    duplicate_conflict -- or the eligibility predicate stopped matching
    between the envelope read and the insert (a race; rare, and the next
    call simply retries).
    """
    existing = (
        await session.execute(
            text(
                "SELECT id, content_sha256, restored_at FROM glacier_messages "
                "WHERE account_id = :account_id AND msg_key = :msg_key"
            ),
            {"account_id": account_id, "msg_key": msg_key},
        )
    ).mappings().one_or_none()
    if existing is None or existing["restored_at"] is not None:
        return CopyOutcome("ineligible", None, account_id, "message state changed, try again")

    live_hash = (
        await session.execute(
            text("SELECT sha256(raw_source) AS h FROM messages WHERE id = :id"),
            {"id": message_id},
        )
    ).mappings().one_or_none()
    if live_hash is not None and live_hash["h"] == existing["content_sha256"]:
        return CopyOutcome("duplicate_removable", existing["id"], account_id)
    return CopyOutcome(
        "duplicate_conflict", existing["id"], account_id,
        "a different message already claims this identity in the glacier",
    )


async def copy_message(db: DatabaseConnection, message_id: uuid.UUID) -> CopyOutcome:
    """
    The copy (design section 3.2): envelope read first, then one
    INSERT ... SELECT that never loads raw_source or attachment bytes
    into Python. Its own committed transaction -- a crash after this
    commits leaves the row `copied` for the next call to verify;
    nothing on the mail server has been touched yet.

    Args:
        db: Database connection
        message_id: The live message to copy

    Returns:
        What happened -- see CopyOutcome
    """
    async with db.session() as session:
        envelope = (
            await session.execute(
                text(
                    f"""
                    SELECT m.id, m.account_id, m.folder_id, m.message_id, m.from_addr,
                           m.subject, m.received_at, m.size_bytes, m.imap_uid, f.imap_name,
                           ap.glacier_folder_id
                    FROM messages m
                    JOIN folders f ON f.id = m.folder_id
                    JOIN accounts a ON a.id = m.account_id
                    LEFT JOIN account_prefs ap
                           ON ap.account_id = m.account_id AND ap.glacier_enabled = true
                    WHERE m.id = :id AND {_ELIGIBILITY_SQL}
                    """
                ),
                {"id": message_id},
            )
        ).mappings().one_or_none()
        if envelope is None:
            reason = await eligibility_reason(session, message_id)
            return CopyOutcome("ineligible", None, None, reason or "message is not eligible")
        if envelope["glacier_folder_id"] is None:
            return CopyOutcome(
                "ineligible", None, envelope["account_id"],
                "glacier is not enabled for this account",
            )

        msg_key = compute_msg_key(
            account_id=envelope["account_id"],
            message_id_hdr=envelope["message_id"],
            from_addr=envelope["from_addr"],
            subject=envelope["subject"],
            received_at=envelope["received_at"],
            size_bytes=envelope["size_bytes"],
        )
        glacier_id = uuid.uuid4()
        inserted = (
            await session.execute(
                text(
                    f"""
                    INSERT INTO glacier_messages (
                        id, account_id, folder_id, imap_uid, thread_id, message_id, subject,
                        from_addr, to_addrs, cc_addrs, bcc_addrs, reply_to, in_reply_to,
                        "references", body_text, body_html, raw_headers, raw_source,
                        is_truncated, received_at, size_bytes, modseq, is_seen, is_flagged,
                        is_answered, is_draft, is_deleted, keywords, expunged_at, created_at,
                        updated_at, msg_key, content_sha256, attachment_count,
                        origin_message_id, origin_folder_id, origin_imap_name,
                        origin_imap_uid, state, visible_at
                    )
                    SELECT
                        :glacier_id, m.account_id, :glacier_folder_id, NULL, m.thread_id,
                        m.message_id, m.subject, m.from_addr, m.to_addrs, m.cc_addrs,
                        m.bcc_addrs, m.reply_to, m.in_reply_to, m.references, m.body_text,
                        m.body_html, m.raw_headers, m.raw_source, false, m.received_at,
                        m.size_bytes, m.modseq, m.is_seen, m.is_flagged, m.is_answered,
                        false, false, m.keywords, NULL, m.created_at, now(), :msg_key,
                        sha256(m.raw_source), 0, m.id, m.folder_id, f.imap_name, m.imap_uid,
                        'copied', NULL
                    FROM messages m
                    JOIN folders f ON f.id = m.folder_id
                    JOIN accounts a ON a.id = m.account_id
                    WHERE m.id = :id AND {_ELIGIBILITY_SQL}
                    ON CONFLICT (account_id, msg_key) DO UPDATE SET
                        folder_id = EXCLUDED.folder_id, imap_uid = NULL,
                        thread_id = EXCLUDED.thread_id, message_id = EXCLUDED.message_id,
                        subject = EXCLUDED.subject, from_addr = EXCLUDED.from_addr,
                        to_addrs = EXCLUDED.to_addrs, cc_addrs = EXCLUDED.cc_addrs,
                        bcc_addrs = EXCLUDED.bcc_addrs, reply_to = EXCLUDED.reply_to,
                        in_reply_to = EXCLUDED.in_reply_to,
                        "references" = EXCLUDED."references",
                        body_text = EXCLUDED.body_text, body_html = EXCLUDED.body_html,
                        raw_headers = EXCLUDED.raw_headers, raw_source = EXCLUDED.raw_source,
                        is_truncated = false, received_at = EXCLUDED.received_at,
                        size_bytes = EXCLUDED.size_bytes, modseq = EXCLUDED.modseq,
                        is_seen = EXCLUDED.is_seen, is_flagged = EXCLUDED.is_flagged,
                        is_answered = EXCLUDED.is_answered, is_draft = false,
                        is_deleted = false, keywords = EXCLUDED.keywords, expunged_at = NULL,
                        updated_at = now(), content_sha256 = EXCLUDED.content_sha256,
                        attachment_count = 0, origin_message_id = EXCLUDED.origin_message_id,
                        origin_folder_id = EXCLUDED.origin_folder_id,
                        origin_imap_name = EXCLUDED.origin_imap_name,
                        origin_imap_uid = EXCLUDED.origin_imap_uid, state = 'copied',
                        visible_at = NULL, restored_at = NULL, restore_outbox_id = NULL,
                        restore_started_at = NULL, last_error = NULL
                    WHERE glacier_messages.restored_at IS NOT NULL
                    RETURNING id, (xmax = 0) AS was_insert
                    """
                ),
                {
                    "id": message_id, "glacier_id": glacier_id,
                    "glacier_folder_id": envelope["glacier_folder_id"], "msg_key": msg_key,
                },
            )
        ).mappings().one_or_none()

        if inserted is None:
            return await _handle_existing_glacier_row(
                session, account_id=envelope["account_id"], msg_key=msg_key,
                message_id=message_id,
            )

        glacier_row_id = inserted["id"]
        if not inserted["was_insert"]:
            # Tombstone refill: drop whatever a prior restore left (there
            # should be nothing -- restore clears attachment rows) before
            # copying the current set, so re-glaciering the same message
            # can never double up.
            await session.execute(
                text("DELETE FROM glacier_attachments WHERE glacier_message_id = :gid"),
                {"gid": glacier_row_id},
            )
        await session.execute(
            text(_GLACIER_ATTACHMENT_COPY_SQL), {"gid": glacier_row_id, "origin_id": message_id},
        )
        count_row = (
            await session.execute(
                text(
                    "SELECT count(*) AS n FROM glacier_attachments WHERE glacier_message_id = :gid"
                ),
                {"gid": glacier_row_id},
            )
        ).mappings().one()
        await session.execute(
            text("UPDATE glacier_messages SET attachment_count = :n WHERE id = :gid"),
            {"n": count_row["n"], "gid": glacier_row_id},
        )
        status: CopyStatus = "copied" if inserted["was_insert"] else "refilled"
        return CopyOutcome(status, glacier_row_id, envelope["account_id"])


async def resolve_duplicate(
    db: DatabaseConnection, message_id: uuid.UUID, existing_glacier_id: uuid.UUID,
) -> bool:
    """
    Section 3.7's "content hash equal" branch: a live message that is
    byte-identical to an already-verified glacier copy. Expunging it is
    provably lossless, so this does it directly -- guarded against the
    *existing* glacier row's own recorded identity, never the live
    message's, so the same expunge_if_matches guard that protects the
    ordinary path protects this one too.

    Args:
        db: Database connection
        message_id: The live duplicate
        existing_glacier_id: The glacier row copy_message reported as
            already holding this identity

    Returns:
        True if the duplicate was expunged
    """
    async with db.session() as session:
        existing = (
            await session.execute(
                text(
                    "SELECT account_id, message_id, size_bytes, received_at "
                    "FROM glacier_messages WHERE id = :gid"
                ),
                {"gid": existing_glacier_id},
            )
        ).mappings().one_or_none()
        if existing is None:
            return False
        rowcount = await expunge_if_matches(
            session, message_id, account_id=existing["account_id"],
            message_id_hdr=existing["message_id"], size_bytes=existing["size_bytes"],
            received_at=existing["received_at"],
        )
        return rowcount == 1


async def verify_message(db: DatabaseConnection, glacier_id: uuid.UUID) -> bool:
    """
    The verify (design section 3.3), its own transaction, deliberately
    separate from the copy that preceded it and the expunge that follows.

    🚨 Both sides of the hash comparison are re-read fresh: the stored
    copy against the live original (proves the bytes actually landed
    correctly), and the stored copy against its own recorded
    content_sha256 (proves it has not been corrupted since). Comparing
    only the recorded hash against the live original would let a stored
    copy corrupted after the INSERT pass silently -- the recorded hash
    was computed from the same bytes that got corrupted, so it agrees
    with itself no matter what.

    Args:
        db: Database connection
        glacier_id: The glacier row to verify

    Returns:
        True if verification passed and the row is now "verified"; False
        if it is not ready yet (stays "copied", retried later) or was not
        in the "copied" state to begin with
    """
    async with db.session() as session:
        result = await session.execute(
            text(
                """
                UPDATE glacier_messages g SET state = 'verified'
                WHERE g.id = :gid AND g.state = 'copied'
                  AND EXISTS (
                    SELECT 1 FROM messages m WHERE m.id = g.origin_message_id
                      AND m.expunged_at IS NULL
                      AND sha256(g.raw_source) = sha256(m.raw_source)
                      AND sha256(g.raw_source) = g.content_sha256
                      AND octet_length(g.raw_source) = octet_length(m.raw_source)
                  )
                  AND g.attachment_count = (
                        SELECT count(*) FROM attachments a
                        WHERE a.message_id = g.origin_message_id
                  )
                  AND NOT EXISTS (
                        SELECT 1 FROM attachments a
                        LEFT JOIN glacier_attachments ga
                               ON ga.glacier_message_id = g.id
                              AND ga.source_attachment_id = a.id
                        WHERE a.message_id = g.origin_message_id
                          AND (
                            ga.id IS NULL
                            OR sha256(coalesce(ga.data, '')) IS DISTINCT FROM
                               sha256(coalesce(a.data, ''))
                          )
                  )
                RETURNING g.id
                """
            ),
            {"gid": glacier_id},
        )
        return (result.rowcount or 0) == 1  # type: ignore[attr-defined]


async def expunge_message(
    db: DatabaseConnection, glacier_id: uuid.UUID, *, event_ring: EventRing | None = None,
) -> ExpungeResult:
    """
    The expunge (design section 3.4) -- the step that actually removes
    the message from the mail server. Requires origin_message_id to be
    set; a row whose origin needs re-resolving (section 3.8) is claimed
    by neither this nor by section 3.4's own guard, and reresolve_origins
    handles it in its own pass instead.

    Args:
        db: Database connection
        glacier_id: The glacier row to expunge, must be "verified"
        event_ring: Optional SSE ring to announce the move on; omitted
            in contexts (a sweep tick with no request in flight, a test)
            that do not need it

    Returns:
        "expunged" on success, "rolled_back" if the identity guard
        refused (the row stays "verified" for reresolve_origins to
        handle), "not_ready" if the row was not claimable at all
    """
    async with db.session() as session:
        claimed = (
            await session.execute(
                text(
                    """
                    UPDATE glacier_messages
                    SET state = 'removing', expunge_requested_at = now(), visible_at = now()
                    WHERE id = :gid AND state = 'verified' AND origin_message_id IS NOT NULL
                    RETURNING origin_message_id, account_id, message_id, size_bytes,
                              received_at, folder_id, origin_folder_id
                    """
                ),
                {"gid": glacier_id},
            )
        ).mappings().one_or_none()
        if claimed is None:
            return "not_ready"

        expunged = await expunge_if_matches(
            session, claimed["origin_message_id"], account_id=claimed["account_id"],
            message_id_hdr=claimed["message_id"], size_bytes=claimed["size_bytes"],
            received_at=claimed["received_at"],
        )
        if expunged == 0:
            # The identity guard refused, or the row vanished under a
            # UIDVALIDITY resync -- the glacier row must never reach
            # "removing" while the live message might still be the
            # intact original. Roll back everything this transaction
            # did and leave it to reresolve_origins's own, later pass.
            await session.rollback()
            return "rolled_back"

        await session.execute(
            text("UPDATE mail_tags SET mail_id = :gid WHERE mail_id = :origin_id"),
            {"gid": glacier_id, "origin_id": claimed["origin_message_id"]},
        )
        await session.execute(
            text("UPDATE verdicts SET mail_id = :gid WHERE mail_id = :origin_id"),
            {"gid": glacier_id, "origin_id": claimed["origin_message_id"]},
        )
        await session.execute(
            text("UPDATE message_embeddings SET message_id = NULL WHERE message_id = :origin_id"),
            {"origin_id": claimed["origin_message_id"]},
        )

    if event_ring is not None:
        old_folder_id = claimed["origin_folder_id"]
        payload = {
            "id": str(glacier_id), "account_id": str(claimed["account_id"]),
            "folder_id": str(claimed["folder_id"]), "changed": ["folder_id"],
        }
        if old_folder_id is not None:
            payload["old_folder_id"] = str(old_folder_id)
        await event_ring.add(claimed["account_id"], "mail.updated", payload)
        await event_ring.add(
            claimed["account_id"], "folder.changed", {"account_id": str(claimed["account_id"])},
        )
    return "expunged"


async def confirm_or_withdraw_removing(
    db: DatabaseConnection, account_id: uuid.UUID, *, grace_seconds: int, batch_size: int = 200,
) -> tuple[int, int]:
    """
    Section 3.5/3.6's bookkeeping over rows sitting in "removing": the
    contract offers no positive signal that an EXPUNGE reached the
    server, so the end is detected by age -- old enough, and no
    sync_notifications row reporting this exact delete failed, means it
    landed. A matching notification means it did not, and section 3.6
    applies: the glacier copy is withdrawn, since the live message is
    authoritative again.

    Detecting the end by age rather than by enumerating every way an
    expunge can end is deliberate -- a guard built as "suppress on each
    known ending" is a set that can never be closed.

    Args:
        db: Database connection
        account_id: Account to sweep
        grace_seconds: How long a row must have sat in "removing" before
            its lack of a failure notification counts as success
        batch_size: How many rows to consider in one tick

    Returns:
        (confirmed_count, withdrawn_count)
    """
    confirmed = 0
    withdrawn = 0
    async with db.session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, origin_message_id, msg_key
                    FROM glacier_messages
                    WHERE account_id = :account_id AND state = 'removing'
                      AND expunge_requested_at < now() - make_interval(secs => :grace)
                    LIMIT :batch
                    """
                ),
                {"account_id": account_id, "grace": grace_seconds, "batch": batch_size},
            )
        ).mappings().all()
        for row in rows:
            failed = (
                await session.execute(
                    text(
                        "SELECT 1 FROM sync_notifications WHERE account_id = :account_id "
                        "AND action = 'delete' AND message_id = :origin_id LIMIT 1"
                    ),
                    {"account_id": account_id, "origin_id": row["origin_message_id"]},
                )
            ).scalar_one_or_none()
            if failed is not None:
                await session.execute(
                    text("UPDATE mail_tags SET mail_id = :origin_id WHERE mail_id = :gid"),
                    {"origin_id": row["origin_message_id"], "gid": row["id"]},
                )
                await session.execute(
                    text("UPDATE verdicts SET mail_id = :origin_id WHERE mail_id = :gid"),
                    {"origin_id": row["origin_message_id"], "gid": row["id"]},
                )
                await session.execute(
                    text(
                        "UPDATE message_embeddings SET message_id = :origin_id "
                        "WHERE message_id IS NULL AND account_id = :account_id "
                        "AND msg_key = :msg_key"
                    ),
                    {
                        "origin_id": row["origin_message_id"], "account_id": account_id,
                        "msg_key": row["msg_key"],
                    },
                )
                await session.execute(
                    text("DELETE FROM glacier_attachments WHERE glacier_message_id = :gid"),
                    {"gid": row["id"]},
                )
                await session.execute(
                    text("DELETE FROM glacier_messages WHERE id = :gid"), {"gid": row["id"]},
                )
                withdrawn += 1
                logger.warning(
                    "Glacier expunge failed on the server, copy withdrawn",
                    extra={"account_id": str(account_id), "glacier_id": str(row["id"])},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE glacier_messages SET state = 'glaciered', glaciered_at = now() "
                        "WHERE id = :gid"
                    ),
                    {"gid": row["id"]},
                )
                confirmed += 1
    return confirmed, withdrawn


async def reresolve_origins(
    db: DatabaseConnection, account_id: uuid.UUID, *, batch_size: int = 200,
) -> int:
    """
    Section 3.8: a "verified" row whose origin_message_id no longer
    resolves to a live row -- a UIDVALIDITY resync deletes and recreates
    a folder's rows with new ids. Re-derived by the same identity a
    verified copy carries (account, Message-ID header, envelope size and
    date) among currently-live rows.

    Found: origin_message_id is rewritten, so the next expunge_message
    call targets the right row. Not found: nothing on the server holds
    this message under any id the mirror knows, so there is nothing left
    to remove -- finalized straight to "glaciered", re-pointing
    tags/verdicts from whatever the last known origin was.

    Args:
        db: Database connection
        account_id: Account to sweep
        batch_size: How many rows to consider in one tick

    Returns:
        How many rows were re-resolved or finalized
    """
    handled = 0
    async with db.session() as session:
        candidates = (
            await session.execute(
                text(
                    """
                    SELECT g.id, g.origin_message_id AS old_origin_id, g.message_id,
                           g.size_bytes, g.received_at
                    FROM glacier_messages g
                    LEFT JOIN messages m
                           ON m.id = g.origin_message_id AND m.expunged_at IS NULL
                    WHERE g.account_id = :account_id AND g.state = 'verified' AND m.id IS NULL
                    LIMIT :batch
                    """
                ),
                {"account_id": account_id, "batch": batch_size},
            )
        ).mappings().all()
        for row in candidates:
            match = (
                await session.execute(
                    text(
                        """
                        SELECT id FROM messages
                        WHERE account_id = :account_id AND expunged_at IS NULL
                          AND coalesce(message_id, '') = coalesce(:message_id_hdr, '')
                          AND size_bytes IS NOT DISTINCT FROM :size_bytes
                          AND received_at IS NOT DISTINCT FROM :received_at
                        LIMIT 1
                        """
                    ),
                    {
                        "account_id": account_id, "message_id_hdr": row["message_id"],
                        "size_bytes": row["size_bytes"], "received_at": row["received_at"],
                    },
                )
            ).mappings().one_or_none()
            if match is not None:
                await session.execute(
                    text(
                        "UPDATE glacier_messages SET origin_message_id = :mid WHERE id = :gid"
                    ),
                    {"mid": match["id"], "gid": row["id"]},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE glacier_messages SET state = 'glaciered', glaciered_at = now(), "
                        "origin_message_id = NULL WHERE id = :gid"
                    ),
                    {"gid": row["id"]},
                )
                if row["old_origin_id"] is not None:
                    await session.execute(
                        text("UPDATE mail_tags SET mail_id = :gid WHERE mail_id = :origin_id"),
                        {"gid": row["id"], "origin_id": row["old_origin_id"]},
                    )
                    await session.execute(
                        text("UPDATE verdicts SET mail_id = :gid WHERE mail_id = :origin_id"),
                        {"gid": row["id"], "origin_id": row["old_origin_id"]},
                    )
            handled += 1
    return handled


async def repair_thread_ids(
    db: DatabaseConnection, account_id: uuid.UUID, *, batch_size: int = 200,
) -> int:
    """
    Section 3.9: a visible glacier row whose thread_id matches no live
    message of this account is re-grouped the way PostIMAP resolves
    threads at insert time -- walk References (closest ancestor first,
    the last entry) then In-Reply-To, against (account_id,
    messages.message_id) -- and adopts the match's thread_id.

    A conversation wholly inside the glacier keeps whatever thread_id it
    had; nothing can re-derive one for it, and nothing needs to, since a
    read never groups it against anything live either.

    Args:
        db: Database connection
        account_id: Account to sweep
        batch_size: How many rows to consider in one tick

    Returns:
        How many rows were regrouped
    """
    repaired = 0
    async with db.session() as session:
        orphans = (
            await session.execute(
                text(
                    """
                    SELECT g.id, g.thread_id, g."references" AS refs, g.in_reply_to
                    FROM glacier_messages g
                    WHERE g.account_id = :account_id AND g.visible_at IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM messages m
                        WHERE m.account_id = g.account_id AND m.thread_id = g.thread_id
                          AND m.expunged_at IS NULL
                      )
                    LIMIT :batch
                    """
                ),
                {"account_id": account_id, "batch": batch_size},
            )
        ).mappings().all()
        for row in orphans:
            candidates = list(reversed(row["refs"] or []))
            if row["in_reply_to"]:
                candidates.append(row["in_reply_to"])
            new_thread = None
            for ref in candidates:
                match = (
                    await session.execute(
                        text(
                            "SELECT thread_id FROM messages WHERE account_id = :account_id "
                            "AND message_id = :ref AND expunged_at IS NULL LIMIT 1"
                        ),
                        {"account_id": account_id, "ref": ref},
                    )
                ).scalar_one_or_none()
                if match is not None:
                    new_thread = match
                    break
            if new_thread is not None and new_thread != row["thread_id"]:
                await session.execute(
                    text("UPDATE glacier_messages SET thread_id = :t WHERE id = :gid"),
                    {"t": new_thread, "gid": row["id"]},
                )
                repaired += 1
    return repaired


async def glacier_message_now(
    db: DatabaseConnection, message_id: uuid.UUID, *, event_ring: EventRing | None = None,
) -> ManualOutcome:
    """
    The manual move action, run to completion synchronously: copy,
    verify, expunge -- each still its own transaction underneath, so a
    failure partway leaves the row in a state the sweep can pick up and
    finish exactly as it would for an automatically-glaciered message.

    Args:
        db: Database connection
        message_id: The message to glacier now
        event_ring: Optional SSE ring, see expunge_message

    Returns:
        Whether it fully completed, and why not if it did not
    """
    capability_error = await require_message_append_support(db)
    if capability_error is not None:
        return ManualOutcome(False, capability_error)

    outcome = await copy_message(db, message_id)
    if outcome.status == "ineligible":
        return ManualOutcome(False, outcome.reason)
    if outcome.status == "duplicate_conflict":
        return ManualOutcome(False, outcome.reason, glacier_id=outcome.glacier_id)
    if outcome.status == "duplicate_removable":
        assert outcome.glacier_id is not None
        ok = await resolve_duplicate(db, message_id, outcome.glacier_id)
        if not ok:
            return ManualOutcome(
                False, "could not confirm the server duplicate", glacier_id=outcome.glacier_id,
            )
        return ManualOutcome(True, None, glacier_id=outcome.glacier_id)

    assert outcome.glacier_id is not None and outcome.account_id is not None
    glacier_id = outcome.glacier_id
    if not await verify_message(db, glacier_id):
        return ManualOutcome(
            True, "copied; verifying and removing from the server in the background",
            glacier_id=glacier_id, pending=True,
        )
    result = await expunge_message(db, glacier_id, event_ring=event_ring)
    if result == "expunged":
        return ManualOutcome(True, None, glacier_id=glacier_id)
    if result == "rolled_back":
        await reresolve_origins(db, outcome.account_id)
        return ManualOutcome(
            True, "the message moved on the server; retrying automatically",
            glacier_id=glacier_id, pending=True,
        )
    return ManualOutcome(
        True, "verified; removing from the server in the background",
        glacier_id=glacier_id, pending=True,
    )

