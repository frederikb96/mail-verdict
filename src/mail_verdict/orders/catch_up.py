"""
The catch-up sweep: looking through recent mail after switching orders on
for an account, or widening the window later -- everything the pipeline
never ran the orders stage against because it arrived before the switch
was on, or was filed straight into Archive by a rule.

Messages are read in fixed-size batches ordered by (received_at, id), the
filter runs off the event loop, and `dry_run` does everything but the
final inserts -- so `POST /api/orders/catch-up` can answer "N mails would
be read" before committing to the cost. Running it again with a longer
window only adds what a `no order_jobs row yet` check still finds; a mail
already queued or already decided is untouched.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Folder, FolderPrefs, Message, OrderJob
from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.orders.content import prepare_body
from mail_verdict.orders.filter import evaluate_filter
from mail_verdict.queue.notify import WorkQueueNotifier

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.service import SettingsService

_BATCH_SIZE = 200
_EXCLUDED_SPECIAL_USE = ("junk", "trash", "drafts", "sent")
_CATCHUP_PRIORITY = 100


@dataclass(frozen=True)
class CatchUpResult:
    considered: int
    passed: int
    queued: int


async def run_catch_up(
    db: DatabaseConnection,
    settings_service: SettingsService,
    *,
    account_id: uuid.UUID,
    days: int,
    dry_run: bool,
) -> CatchUpResult:
    """
    Args:
        account_id: Account to sweep
        days: How many days back to look, by received_at
        dry_run: When True, count what would happen without inserting
            anything

    Returns:
        considered: messages examined; passed: how many passed the
        filter; queued: how many got a new order_jobs row (equals
        `passed` unless dry_run, in which case it is always 0)
    """
    patterns = settings_service.get("orders").get("filter", {})
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    effective_special_use = func.coalesce(FolderPrefs.special_use_override, Folder.special_use)

    considered = 0
    passed = 0
    queued = 0
    last_key: tuple[datetime, uuid.UUID] | None = None

    async with db.session() as session:
        while True:
            stmt = (
                select(
                    Message.id, Message.received_at, Message.subject, Message.from_addr,
                    Message.body_text, Message.body_html, Message.message_id, Message.size_bytes,
                )
                .select_from(Message)
                .join(Folder, Folder.id == Message.folder_id)
                .outerjoin(FolderPrefs, Folder.id == FolderPrefs.folder_id)
                .where(
                    Message.account_id == account_id, Message.expunged_at.is_(None),
                    Message.is_draft.is_(False), Message.received_at >= cutoff,
                    or_(
                        effective_special_use.is_(None),
                        ~effective_special_use.in_(_EXCLUDED_SPECIAL_USE),
                    ),
                )
                .order_by(Message.received_at, Message.id)
                .limit(_BATCH_SIZE)
            )
            if last_key is not None:
                stmt = stmt.where(
                    or_(
                        Message.received_at > last_key[0],
                        and_(Message.received_at == last_key[0], Message.id > last_key[1]),
                    )
                )
            rows = (await session.execute(stmt)).all()
            if not rows:
                break

            last_key = (rows[-1].received_at, rows[-1].id)
            considered += len(rows)

            mail_ids = [row.id for row in rows]
            spam_ids = await _spam_mail_ids(session, mail_ids)

            keys = {
                row.id: compute_msg_key(
                    account_id=account_id, message_id_hdr=row.message_id,
                    from_addr=row.from_addr, subject=row.subject,
                    received_at=row.received_at, size_bytes=row.size_bytes,
                )
                for row in rows
            }
            already_queued = await _already_queued_keys(session, account_id, set(keys.values()))

            candidates = [
                row for row in rows
                if row.id not in spam_ids and keys[row.id] not in already_queued
            ]

            def _filter_batch() -> list[Any]:
                out = []
                for row in candidates:
                    prepared = prepare_body(body_text=row.body_text, body_html=row.body_html)
                    result = evaluate_filter(
                        subject=row.subject or "", from_header=row.from_addr or "",
                        body=prepared.text, patterns=patterns,
                    )
                    if result.passed:
                        out.append(row)
                return out

            batch_passed = await asyncio.to_thread(_filter_batch)
            passed += len(batch_passed)

            if not dry_run and batch_passed:
                for row in batch_passed:
                    insert_stmt = (
                        pg_insert(OrderJob)
                        .values(
                            kind="mail", account_id=account_id, msg_key=keys[row.id],
                            message_id=row.id, origin="catchup", priority=_CATCHUP_PRIORITY,
                            next_attempt_at=row.received_at,
                        )
                        .on_conflict_do_nothing(
                            index_elements=["account_id", "msg_key"],
                            index_where=text("kind = 'mail'"),
                        )
                    )
                    result = await session.execute(insert_stmt)
                    if result.rowcount:  # type: ignore[attr-defined]
                        queued += 1
                await WorkQueueNotifier.notify(session, "orders")

    return CatchUpResult(considered=considered, passed=passed, queued=queued)


async def _spam_mail_ids(session: AsyncSession, mail_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Every mail_id in the batch whose current verdict (latest
    user_feedback if any, else latest by created_at) is spam."""
    if not mail_ids:
        return set()
    result = await session.execute(
        text(
            """
            SELECT DISTINCT ON (mail_id) mail_id, is_spam
            FROM verdicts
            WHERE mail_id = ANY(:ids)
            ORDER BY mail_id, (source = 'user_feedback') DESC, created_at DESC
            """
        ),
        {"ids": mail_ids},
    )
    return {row.mail_id for row in result.all() if row.is_spam}


async def _already_queued_keys(
    session: AsyncSession, account_id: uuid.UUID, msg_keys: set[str],
) -> set[str]:
    if not msg_keys:
        return set()
    result = await session.execute(
        select(OrderJob.msg_key).where(
            OrderJob.account_id == account_id, OrderJob.msg_key.in_(msg_keys),
        )
    )
    return {row[0] for row in result.all()}
