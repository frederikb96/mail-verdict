"""Tests for parsing postimap_events NOTIFY payloads."""

from __future__ import annotations

from mail_verdict.postimap.listener import PostimapEvent


class TestOldFolderIdParsing:
    """Tests for the move-specific old_folder_id field.

    The contract's own gotcha: absent means "not a folder move", present
    means the source folder -- never null. A parser that used raw["..."]
    or treated a missing key as an error would break on every non-move
    update event, which is most of them.
    """

    def test_absent_when_key_is_missing(self) -> None:
        """A payload with no old_folder_id key at all parses to None, not an error."""
        event = PostimapEvent.from_payload({
            "v": 1, "type": "message", "op": "update", "id": "m1",
            "account_id": "a1", "folder_id": "f1", "changed": ["is_seen"],
        })
        assert event.old_folder_id is None

    def test_present_when_key_is_a_folder_id(self) -> None:
        """A payload naming the source folder of a move parses it through."""
        event = PostimapEvent.from_payload({
            "v": 1, "type": "message", "op": "update", "id": "m1",
            "account_id": "a1", "folder_id": "dest-folder",
            "changed": ["folder_id", "imap_uid"], "old_folder_id": "source-folder",
        })
        assert event.old_folder_id == "source-folder"


class TestMinimalRequiredPayload:
    """Tests that optional fields never become required just because one exists."""

    def test_insert_payload_with_no_optional_fields_parses(self) -> None:
        """A bare insert event (no changed, no origin, no old_folder_id) still parses."""
        event = PostimapEvent.from_payload({
            "v": 1, "type": "message", "op": "insert", "id": "m1",
            "account_id": "a1", "folder_id": "f1",
        })
        assert event.old_folder_id is None
        assert event.changed == ()


class TestDavEventParsing:
    """DAV events carry account_id = dav_accounts.id, never folder_id."""

    def test_dav_account_insert_has_no_collection_id(self) -> None:
        event = PostimapEvent.from_payload({
            "v": 1, "type": "dav_account", "op": "insert", "id": "acc1",
            "account_id": "acc1", "origin": "app",
        })
        assert event.type == "dav_account"
        assert event.folder_id is None
        assert event.collection_id is None

    def test_dav_object_move_carries_old_collection_id(self) -> None:
        event = PostimapEvent.from_payload({
            "v": 1, "type": "dav_object", "op": "update", "id": "obj1",
            "account_id": "acc1", "collection_id": "coll-dest",
            "old_collection_id": "coll-source", "changed": ["collection_id"],
        })
        assert event.collection_id == "coll-dest"
        assert event.old_collection_id == "coll-source"

    def test_dav_notification_carries_action(self) -> None:
        event = PostimapEvent.from_payload({
            "v": 1, "type": "dav_notification", "op": "insert", "id": "42",
            "account_id": "acc1", "collection_id": "coll1", "action": "put",
        })
        assert event.action == "put"
        assert event.old_collection_id is None
