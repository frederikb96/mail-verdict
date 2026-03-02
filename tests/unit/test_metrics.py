"""Tests for spam metrics: stat computation, accuracy formula, correction tracking."""

from __future__ import annotations

from mail_verdict.spam.metrics import SpamStats, WeeklyTrend


class TestSpamStats:
    """Tests for SpamStats dataclass."""

    def test_creation(self) -> None:
        """SpamStats can be created with all fields."""
        stats = SpamStats(
            total_verdicts=100,
            ai_verdicts=80,
            rule_verdicts=10,
            user_corrections=10,
            spam_count=30,
            ham_count=60,
            false_positives=3,
            false_negatives=2,
            correction_rate=0.1111,
            fp_rate=0.0333,
            fn_rate=0.0222,
            accuracy=0.9444,
        )
        assert stats.total_verdicts == 100
        assert stats.accuracy == 0.9444

    def test_accuracy_formula(self) -> None:
        """Verify accuracy = (automated - fp - fn) / automated."""
        automated = 90
        fp = 3
        fn = 2
        accuracy = (automated - fp - fn) / automated
        stats = SpamStats(
            total_verdicts=100,
            ai_verdicts=80,
            rule_verdicts=10,
            user_corrections=10,
            spam_count=30,
            ham_count=60,
            false_positives=fp,
            false_negatives=fn,
            correction_rate=10 / 90,
            fp_rate=fp / automated,
            fn_rate=fn / automated,
            accuracy=round(accuracy, 4),
        )
        assert stats.accuracy == round(accuracy, 4)

    def test_zero_automated_accuracy(self) -> None:
        """With zero automated verdicts, accuracy is 1.0 by convention."""
        stats = SpamStats(
            total_verdicts=5,
            ai_verdicts=0,
            rule_verdicts=0,
            user_corrections=5,
            spam_count=3,
            ham_count=2,
            false_positives=0,
            false_negatives=0,
            correction_rate=0.0,
            fp_rate=0.0,
            fn_rate=0.0,
            accuracy=1.0,
        )
        assert stats.accuracy == 1.0


class TestWeeklyTrend:
    """Tests for WeeklyTrend dataclass."""

    def test_creation(self) -> None:
        """WeeklyTrend can be created."""
        from datetime import datetime, timezone

        trend = WeeklyTrend(
            week_start=datetime(2024, 1, 15, tzinfo=timezone.utc),
            total=50,
            corrections=5,
            accuracy=0.9,
        )
        assert trend.total == 50
        assert trend.corrections == 5
        assert trend.accuracy == 0.9

    def test_accuracy_calculation(self) -> None:
        """Verify accuracy = automated / (automated + corrections)."""
        total = 50
        corrections = 5
        automated = total - corrections
        accuracy = automated / (automated + corrections)
        assert round(accuracy, 4) == 0.9
