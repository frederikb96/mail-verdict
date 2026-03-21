"""Extended tests for change detection: ChangeSet, SelectInfo, flag parsing."""

from __future__ import annotations

from mail_verdict.sync.change_detector import (
    ChangeSet,
    SelectInfo,
    UIDFlags,
    _parse_uid_set,
)


class TestChangeSetDataclass:
    """Tests for ChangeSet dataclass."""

    def test_empty_changeset(self) -> None:
        """Default ChangeSet has empty lists and no state changes."""
        cs = ChangeSet()
        assert cs.new_uids == []
        assert cs.deleted_uids == []
        assert cs.flag_changes == []
        assert cs.uidvalidity_changed is False
        assert cs.new_uidvalidity is None
        assert cs.new_highestmodseq is None
        assert cs.new_uidnext is None

    def test_changeset_with_new_uids(self) -> None:
        """ChangeSet with new UIDs."""
        cs = ChangeSet(new_uids=[1, 2, 3])
        assert len(cs.new_uids) == 3

    def test_changeset_with_deleted_uids(self) -> None:
        """ChangeSet with deleted UIDs."""
        cs = ChangeSet(deleted_uids=[10, 20])
        assert len(cs.deleted_uids) == 2

    def test_changeset_uidvalidity_changed(self) -> None:
        """ChangeSet marking UIDVALIDITY change."""
        cs = ChangeSet(
            uidvalidity_changed=True,
            new_uidvalidity=42,
        )
        assert cs.uidvalidity_changed is True
        assert cs.new_uidvalidity == 42

    def test_changeset_with_flag_changes(self) -> None:
        """ChangeSet with flag change entries."""
        cs = ChangeSet(
            flag_changes=[
                UIDFlags(uid=1, flags={"\\Seen"}),
                UIDFlags(uid=2, flags={"\\Flagged"}),
            ]
        )
        assert len(cs.flag_changes) == 2
        assert cs.flag_changes[0].uid == 1

    def test_changeset_with_metadata(self) -> None:
        """ChangeSet with full metadata."""
        cs = ChangeSet(
            new_uidvalidity=100,
            new_highestmodseq=500,
            new_uidnext=200,
        )
        assert cs.new_uidvalidity == 100
        assert cs.new_highestmodseq == 500
        assert cs.new_uidnext == 200


class TestSelectInfo:
    """Tests for SelectInfo dataclass."""

    def test_default_values(self) -> None:
        """SelectInfo defaults to zeroes and None modseq."""
        si = SelectInfo(ok=True)
        assert si.uidvalidity == 0
        assert si.uidnext == 0
        assert si.exists == 0
        assert si.highestmodseq is None

    def test_failed_select(self) -> None:
        """SelectInfo with ok=False."""
        si = SelectInfo(ok=False)
        assert si.ok is False

    def test_full_metadata(self) -> None:
        """SelectInfo with full metadata."""
        si = SelectInfo(
            ok=True,
            uidvalidity=12345,
            uidnext=500,
            exists=100,
            highestmodseq=999,
        )
        assert si.ok is True
        assert si.uidvalidity == 12345
        assert si.uidnext == 500
        assert si.exists == 100
        assert si.highestmodseq == 999


class TestUIDFlagsExtended:
    """Extended tests for UIDFlags dataclass."""

    def test_seen_flag(self) -> None:
        """Detect \\Seen in flags."""
        uf = UIDFlags(uid=1, flags={"\\Seen", "\\Flagged"})
        assert "\\Seen" in uf.flags
        assert "\\Flagged" in uf.flags

    def test_empty_flags(self) -> None:
        """Empty flag set."""
        uf = UIDFlags(uid=1, flags=set())
        assert len(uf.flags) == 0

    def test_flags_immutable_check(self) -> None:
        """Flags is a set (mutable, but typed as set)."""
        uf = UIDFlags(uid=1, flags={"\\Seen"})
        uf.flags.add("\\Flagged")
        assert "\\Flagged" in uf.flags

    def test_modseq_optional(self) -> None:
        """modseq defaults to None."""
        uf = UIDFlags(uid=42, flags=set())
        assert uf.modseq is None

    def test_modseq_set(self) -> None:
        """modseq can be set explicitly."""
        uf = UIDFlags(uid=42, flags=set(), modseq=12345)
        assert uf.modseq == 12345


class TestParseUidSetExtended:
    """Extended tests for _parse_uid_set."""

    def test_large_range(self) -> None:
        """Parse a large UID range."""
        result = _parse_uid_set("* VANISHED 1:100")
        assert len(result) == 100
        assert result[0] == 1
        assert result[-1] == 100

    def test_single_uid_at_end(self) -> None:
        """Parse range with single UID appended."""
        result = _parse_uid_set("* VANISHED 1:3,100")
        assert result == [1, 2, 3, 100]

    def test_multiple_ranges(self) -> None:
        """Parse multiple disjoint ranges."""
        result = _parse_uid_set("* VANISHED 1:2,10:12,20:22")
        assert result == [1, 2, 10, 11, 12, 20, 21, 22]

    def test_whitespace_handling(self) -> None:
        """Handles whitespace in UID set."""
        result = _parse_uid_set("* VANISHED  1:3 , 10 ")
        assert 1 in result
        assert 10 in result

    def test_empty_vanished(self) -> None:
        """Empty text after VANISHED returns empty."""
        result = _parse_uid_set("not a vanished response at all")
        assert result == []
