"""The native push envelope: the fixed test vector the iOS app shares, and
the properties a phone relies on when it opens one."""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mail_verdict.database.models import Alert
from mail_verdict.push.envelope import (
    MAX_BLOB_CHARS,
    EnvelopeError,
    alert_payload,
    open_bytes,
    open_payload,
    seal_bytes,
    seal_payload,
)

VECTOR = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "push_envelope_v1.json").read_text(
        encoding="utf-8",
    )
)
KEY = base64.b64decode(VECTOR["content_key_base64"])
INSTALLATION = uuid.UUID(VECTOR["installation_id"])


class TestVector:
    def test_sealing_the_vector_plaintext_reproduces_the_vector_blob(self) -> None:
        blob = seal_bytes(
            VECTOR["plaintext"].encode("utf-8"), KEY, INSTALLATION,
            nonce=bytes.fromhex(VECTOR["nonce_hex"]),
        )
        assert blob == VECTOR["blob"]

    def test_the_vector_blob_has_the_documented_layout(self) -> None:
        """Decrypted by hand from the format description rather than through
        the module, so the vector pins the format and not merely whatever the
        module happens to do."""
        raw = base64.b64decode(VECTOR["blob"])
        assert raw[0] == 1
        assert raw[1:13] == bytes.fromhex(VECTOR["nonce_hex"])
        plaintext = AESGCM(KEY).decrypt(raw[1:13], raw[13:], VECTOR["aad"].encode("ascii"))
        assert plaintext.decode("utf-8") == VECTOR["plaintext"]


class TestOpening:
    def test_a_blob_sealed_for_one_installation_does_not_open_on_another(self) -> None:
        blob = seal_bytes(b"{}", KEY, INSTALLATION)
        with pytest.raises(EnvelopeError):
            open_bytes(blob, KEY, uuid.uuid4())

    def test_a_tampered_blob_is_refused(self) -> None:
        raw = bytearray(base64.b64decode(seal_bytes(b"{}", KEY, INSTALLATION)))
        raw[-1] ^= 0x01
        with pytest.raises(EnvelopeError):
            open_bytes(base64.b64encode(bytes(raw)).decode(), KEY, INSTALLATION)

    def test_an_unknown_version_is_refused(self) -> None:
        raw = bytearray(base64.b64decode(seal_bytes(b"{}", KEY, INSTALLATION)))
        raw[0] = 2
        with pytest.raises(EnvelopeError):
            open_bytes(base64.b64encode(bytes(raw)).decode(), KEY, INSTALLATION)

    def test_each_seal_uses_a_fresh_nonce(self) -> None:
        assert seal_bytes(b"{}", KEY, INSTALLATION) != seal_bytes(b"{}", KEY, INSTALLATION)


class TestAlertPayload:
    def _alert(self, **overrides: object) -> Alert:
        fields: dict[str, object] = {
            "id": uuid.uuid4(), "kind": "mail", "title": "Subject", "body": "sender@example.com",
            "url": "/?message=x", "account_id": uuid.uuid4(), "message_id": uuid.uuid4(),
            "folder_id": uuid.uuid4(),
        }
        fields.update(overrides)
        return Alert(**fields)

    def test_long_subject_and_sender_are_clipped_to_their_limits(self) -> None:
        payload = alert_payload(
            self._alert(title="s" * 500, body="b" * 500), badge=0, resolved=[],
        )
        assert len(payload["title"]) == 160
        assert len(payload["body"]) == 120

    def test_an_oversized_payload_sheds_resolved_ids_rather_than_the_notification(self) -> None:
        """A title near its limit plus twenty resolved ids does not fit; the
        notification itself must survive, the withdraw hints give way."""
        resolved = [uuid.uuid4() for _ in range(20)]
        payload = alert_payload(
            self._alert(title="ü" * 160, body="ü" * 120, url="/" + "u" * 1200),
            badge=7, resolved=resolved,
        )
        blob = seal_payload(payload, KEY, INSTALLATION)

        assert len(blob) <= MAX_BLOB_CHARS
        opened = open_payload(blob, KEY, INSTALLATION)
        assert opened["title"] == payload["title"]
        assert opened["badge"] == 7
        assert 0 < len(opened["resolved"]) < 20
        # What is kept is the newest end of the list.
        assert opened["resolved"] == [str(r) for r in resolved[: len(opened["resolved"])]]
