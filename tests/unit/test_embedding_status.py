"""
EmbeddingStatus.outstanding: the "has every in-scope message been tried
at least once" count CutoverReadiness relies on -- a pure computation,
tested here without a database.
"""

from __future__ import annotations

from mail_verdict.embeddings.repository import EmbeddingStatus


def _status(
    *, in_scope: int, encoded: int = 0, pending: int = 0, failed: int = 0, shadowed: int = 0,
) -> EmbeddingStatus:
    return EmbeddingStatus(
        model="m", in_scope=in_scope, encoded=encoded, pending=pending, failed=failed,
        reachable=encoded, unreachable=0, shadowed=shadowed,
    )


class TestOutstanding:
    def test_nothing_accounted_for_yet(self) -> None:
        assert _status(in_scope=5).outstanding == 5

    def test_everything_done(self) -> None:
        assert _status(in_scope=5, encoded=5).outstanding == 0

    def test_a_mix_of_done_pending_and_failed_sums_to_zero_outstanding(self) -> None:
        assert _status(in_scope=5, encoded=2, pending=1, failed=2).outstanding == 0

    def test_permanently_failed_rows_count_as_tried_not_outstanding(self) -> None:
        """The whole reason this exists: a real mailbox always has a few
        permanently-failed messages, and they must not keep this above
        zero forever."""
        assert _status(in_scope=5, encoded=3, failed=2).outstanding == 0

    def test_shadowed_messages_count_as_covered_via_their_sibling(self) -> None:
        """A shadowed message never gets its own row -- its sibling's row
        is what accounts for it, so it must not read as outstanding."""
        assert _status(in_scope=5, encoded=4, shadowed=1).outstanding == 0

    def test_some_still_genuinely_untried(self) -> None:
        assert _status(in_scope=10, encoded=6, pending=1, failed=1).outstanding == 2

    def test_never_negative(self) -> None:
        """Counts drift slightly across the several sequential queries
        status() runs (not one atomic snapshot) -- a race must read as
        zero outstanding, never a negative number."""
        assert _status(in_scope=5, encoded=6).outstanding == 0
