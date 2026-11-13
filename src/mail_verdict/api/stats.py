"""
Stats/dashboard API endpoint.

GET /api/stats — returns total messages, spam caught, FP/FN rates, sync status.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from mail_verdict.api.deps import current_scope
from mail_verdict.api.schemas import (
    AccountSyncStatus,
    StatsResponse,
    WeeklyTrendPoint,
)
from mail_verdict.auth.ownership import account_display_name
from mail_verdict.auth.scope import Scope
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Folder, Message
from mail_verdict.spam.metrics import SpamMetrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("", response_model=StatsResponse)
async def get_stats(
    account_id: uuid.UUID | None = Query(default=None),
    scope: Scope = Depends(current_scope),
) -> StatsResponse:
    """
    Get dashboard statistics.

    Returns spam metrics, sync status, and weekly accuracy trend.
    If account_id is provided, scopes to that account (an account that is
    not the caller's counts as no account at all). Otherwise, aggregates
    across all of the caller's accounts.
    """
    db = get_db_connection()
    metrics = SpamMetrics(db)

    # The caller's accounts, narrowed to the one asked for
    async with db.session() as session:
        result = await session.execute(
            select(Account)
            .where(Account.id.in_(scope.narrow(account_id)))
            .order_by(Account.name)
        )
        accounts = list(result.scalars().all())

    account_ids = [a.id for a in accounts]

    # Aggregate spam stats
    total_spam = 0
    total_ham = 0
    total_fp = 0
    total_fn = 0
    total_accuracy_sum = 0.0
    total_accuracy_count = 0
    all_trends: list[WeeklyTrendPoint] = []

    for aid in account_ids:
        stats = await metrics.get_stats(aid)
        total_spam += stats.spam_count
        total_ham += stats.ham_count
        total_fp += stats.false_positives
        total_fn += stats.false_negatives
        if stats.total_verdicts > 0:
            total_accuracy_sum += stats.accuracy
            total_accuracy_count += 1

        trends = await metrics.get_weekly_trend(aid)
        for t in trends:
            all_trends.append(
                WeeklyTrendPoint(
                    week_start=t.week_start,
                    total=t.total,
                    corrections=t.corrections,
                    accuracy=t.accuracy,
                )
            )

    # Total message count
    async with db.session() as session:
        msg_count_stmt = select(func.count(Message.id)).where(
            Message.account_id.in_(account_ids)
        )
        count_result = await session.execute(msg_count_stmt)
        total_messages: int = count_result.scalar_one()

    # Per-account sync status
    account_sync: list[AccountSyncStatus] = []
    for acc in accounts:
        async with db.session() as session:
            folder_result = await session.execute(select(Folder).where(Folder.account_id == acc.id))
            folders = list(folder_result.scalars().all())

            msg_result = await session.execute(
                select(func.count(Message.id)).where(Message.account_id == acc.id)
            )
            acc_msg_count = msg_result.scalar_one()

        last_sync = max(
            (f.last_synced_at for f in folders if f.last_synced_at),
            default=None,
        )
        account_sync.append(
            AccountSyncStatus(
                account_id=acc.id,
                account_name=account_display_name(acc.name),
                last_synced_at=last_sync,
                folder_count=len(folders),
                message_count=acc_msg_count,
            )
        )

    automated = total_spam + total_ham
    fp_rate = total_fp / automated if automated > 0 else 0.0
    fn_rate = total_fn / automated if automated > 0 else 0.0
    accuracy = total_accuracy_sum / total_accuracy_count if total_accuracy_count > 0 else 1.0

    return StatsResponse(
        total_messages=total_messages,
        total_accounts=len(accounts),
        spam_caught=total_spam,
        ham_count=total_ham,
        false_positives=total_fp,
        false_negatives=total_fn,
        fp_rate=round(fp_rate, 4),
        fn_rate=round(fn_rate, 4),
        accuracy=round(accuracy, 4),
        weekly_trend=all_trends,
        account_sync=account_sync,
    )
