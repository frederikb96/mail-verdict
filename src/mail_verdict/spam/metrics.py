"""
Spam metrics computation layer.

Computes correction rates, FP/FN rates, accuracy trends,
and rule match counts from the Verdict table.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, desc, func, select

from mail_verdict.database.models import Mail, Verdict, VerdictSource

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


@dataclass
class SpamStats:
    """Aggregate spam detection statistics for an account."""

    total_verdicts: int
    ai_verdicts: int
    rule_verdicts: int
    user_corrections: int
    spam_count: int
    ham_count: int
    false_positives: int
    false_negatives: int
    correction_rate: float
    fp_rate: float
    fn_rate: float
    accuracy: float


@dataclass
class WeeklyTrend:
    """Weekly accuracy trend data point."""

    week_start: datetime
    total: int
    corrections: int
    accuracy: float


class SpamMetrics:
    """
    Computes spam detection metrics from the Verdict table.

    All queries are scoped by account_id.
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize metrics computer.

        Args:
            db: Database connection for queries
        """
        self._db = db

    async def get_stats(
        self,
        account_id: uuid.UUID,
        *,
        since: datetime | None = None,
    ) -> SpamStats:
        """
        Compute aggregate spam statistics for an account.

        False positives = AI said spam, user corrected to not-spam.
        False negatives = AI said not-spam, user corrected to spam.

        Args:
            account_id: Account scope
            since: Optional lower bound on verdict creation time

        Returns:
            SpamStats with rates and counts
        """
        async with self._db.session() as session:
            # Base filter
            base_filter = [Mail.account_id == account_id]
            if since:
                base_filter.append(Verdict.created_at >= since)

            # Total verdicts by source
            source_stmt = (
                select(
                    Verdict.source,
                    func.count(Verdict.id).label("cnt"),
                )
                .join(Mail, Verdict.mail_id == Mail.id)
                .where(*base_filter)
                .group_by(Verdict.source)
            )
            source_result = await session.execute(source_stmt)
            source_counts: dict[str, int] = {}
            for row in source_result:
                source_counts[row.source.value] = row.cnt

            ai_count = source_counts.get(VerdictSource.AI.value, 0)
            rule_count = source_counts.get(VerdictSource.RULE.value, 0)
            user_count = source_counts.get(VerdictSource.USER_FEEDBACK.value, 0)
            total = ai_count + rule_count + user_count

            # Spam vs ham (latest verdict per mail)
            latest_verdict = (
                select(
                    Verdict.mail_id,
                    Verdict.is_spam,
                    func.row_number()
                    .over(
                        partition_by=Verdict.mail_id,
                        order_by=desc(Verdict.created_at),
                    )
                    .label("rn"),
                )
                .join(Mail, Verdict.mail_id == Mail.id)
                .where(*base_filter)
                .subquery()
            )

            spam_ham_stmt = select(
                func.count(latest_verdict.c.mail_id).label("total"),
                func.count(case((latest_verdict.c.is_spam.is_(True), 1))).label("spam"),
                func.count(case((latest_verdict.c.is_spam.is_(False), 1))).label("ham"),
            ).where(latest_verdict.c.rn == 1)

            sh_result = await session.execute(spam_ham_stmt)
            sh_row = sh_result.one()

            # FP/FN: find mails where AI verdict was overridden by user_feedback
            fp_fn = await self._compute_fp_fn(session, account_id, since)

            # Compute rates
            automated = ai_count + rule_count
            correction_rate = user_count / automated if automated > 0 else 0.0
            fp_rate = fp_fn["fp"] / automated if automated > 0 else 0.0
            fn_rate = fp_fn["fn"] / automated if automated > 0 else 0.0
            correct = automated - fp_fn["fp"] - fp_fn["fn"]
            accuracy = correct / automated if automated > 0 else 1.0

            return SpamStats(
                total_verdicts=total,
                ai_verdicts=ai_count,
                rule_verdicts=rule_count,
                user_corrections=user_count,
                spam_count=sh_row.spam,
                ham_count=sh_row.ham,
                false_positives=fp_fn["fp"],
                false_negatives=fp_fn["fn"],
                correction_rate=round(correction_rate, 4),
                fp_rate=round(fp_rate, 4),
                fn_rate=round(fn_rate, 4),
                accuracy=round(accuracy, 4),
            )

    async def get_weekly_trend(
        self,
        account_id: uuid.UUID,
        *,
        weeks: int = 8,
    ) -> list[WeeklyTrend]:
        """
        Compute weekly accuracy trend.

        Args:
            account_id: Account scope
            weeks: Number of weeks to look back

        Returns:
            List of WeeklyTrend data points, newest first
        """
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)

        async with self._db.session() as session:
            # Weekly automated verdicts + user corrections
            week_trunc = func.date_trunc("week", Verdict.created_at)

            stmt = (
                select(
                    week_trunc.label("week_start"),
                    func.count(Verdict.id).label("total"),
                    func.count(
                        case(
                            (Verdict.source == VerdictSource.USER_FEEDBACK, 1),
                        )
                    ).label("corrections"),
                )
                .join(Mail, Verdict.mail_id == Mail.id)
                .where(
                    Mail.account_id == account_id,
                    Verdict.created_at >= cutoff,
                )
                .group_by(week_trunc)
                .order_by(desc(week_trunc))
            )

            result = await session.execute(stmt)
            trends: list[WeeklyTrend] = []
            for row in result:
                automated = row.total - row.corrections
                accuracy = automated / (automated + row.corrections) if automated > 0 else 0.0
                trends.append(
                    WeeklyTrend(
                        week_start=row.week_start,
                        total=row.total,
                        corrections=row.corrections,
                        accuracy=round(max(0.0, accuracy), 4),
                    )
                )

            return trends

    async def get_rule_hit_counts(
        self,
        account_id: uuid.UUID,
        *,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """
        Count verdicts produced by rules (source=rule).

        Groups by the model_used field which stores the rule name.

        Args:
            account_id: Account scope
            since: Optional lower bound

        Returns:
            Dict mapping rule name to hit count
        """
        async with self._db.session() as session:
            filters = [
                Mail.account_id == account_id,
                Verdict.source == VerdictSource.RULE,
            ]
            if since:
                filters.append(Verdict.created_at >= since)

            stmt = (
                select(
                    Verdict.model_used.label("rule_name"),
                    func.count(Verdict.id).label("cnt"),
                )
                .join(Mail, Verdict.mail_id == Mail.id)
                .where(*filters)
                .group_by(Verdict.model_used)
                .order_by(desc(func.count(Verdict.id)))
            )

            result = await session.execute(stmt)
            return {row.rule_name or "unknown": row.cnt for row in result}

    async def _compute_fp_fn(
        self,
        session: Any,
        account_id: uuid.UUID,
        since: datetime | None,
    ) -> dict[str, int]:
        """
        Compute false positive and false negative counts.

        FP: AI/rule verdict was spam, later user_feedback verdict was not-spam.
        FN: AI/rule verdict was not-spam, later user_feedback verdict was spam.

        Args:
            session: Active database session
            account_id: Account scope
            since: Optional lower bound

        Returns:
            Dict with 'fp' and 'fn' counts
        """
        from sqlalchemy.orm import aliased

        ai_verdict = aliased(Verdict)
        user_verdict = aliased(Verdict)

        filters = [
            Mail.account_id == account_id,
            ai_verdict.source.in_([VerdictSource.AI, VerdictSource.RULE]),
            user_verdict.source == VerdictSource.USER_FEEDBACK,
            user_verdict.created_at > ai_verdict.created_at,
            ai_verdict.mail_id == user_verdict.mail_id,
        ]
        if since:
            filters.append(ai_verdict.created_at >= since)

        # FP: AI said spam, user said not-spam
        fp_stmt = (
            select(func.count(func.distinct(ai_verdict.mail_id)))
            .select_from(ai_verdict)
            .join(user_verdict, ai_verdict.mail_id == user_verdict.mail_id)
            .join(Mail, ai_verdict.mail_id == Mail.id)
            .where(
                *filters,
                ai_verdict.is_spam.is_(True),
                user_verdict.is_spam.is_(False),
            )
        )

        # FN: AI said not-spam, user said spam
        fn_stmt = (
            select(func.count(func.distinct(ai_verdict.mail_id)))
            .select_from(ai_verdict)
            .join(user_verdict, ai_verdict.mail_id == user_verdict.mail_id)
            .join(Mail, ai_verdict.mail_id == Mail.id)
            .where(
                *filters,
                ai_verdict.is_spam.is_(False),
                user_verdict.is_spam.is_(True),
            )
        )

        fp_result = await session.execute(fp_stmt)
        fn_result = await session.execute(fn_stmt)

        return {
            "fp": fp_result.scalar_one(),
            "fn": fn_result.scalar_one(),
        }
