"""
The sealed envelope a native push carries: what the phone's notification
extension decrypts into the banner it shows.

Format, version 1:

    blob = base64( 0x01 || nonce (12 bytes) || AES-256-GCM ciphertext || tag (16 bytes) )
    AAD  = b"mv-push-v1:" + installation_id as its lowercase hyphenated string
    key  = the 32-byte content key the device generated and registered

The plaintext is a JSON object (alert_payload). Binding the installation id
into the AAD means a blob sealed for one device never opens on another,
even if two devices were ever handed the same key.

tests/fixtures/push_envelope_v1.json is a fixed test vector; the iOS app's
own tests open the same bytes.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from mail_verdict.database.models import Alert

ENVELOPE_VERSION = 1
CONTENT_KEY_BYTES = 32
NONCE_BYTES = 12
_AAD_PREFIX = b"mv-push-v1:"

# The relay accepts a blob of up to 3072 characters; this leaves headroom.
MAX_BLOB_CHARS = 3000
MAX_TITLE_CHARS = 160
MAX_BODY_CHARS = 120
MAX_RESOLVED_IDS = 20


class EnvelopeError(Exception):
    """A blob that is malformed, of an unknown version, or fails authentication."""


def _aad(installation_id: uuid.UUID) -> bytes:
    return _AAD_PREFIX + str(installation_id).encode("ascii")


def seal_bytes(
    plaintext: bytes, key: bytes, installation_id: uuid.UUID, *, nonce: bytes | None = None,
) -> str:
    """
    Seal raw plaintext into a blob.

    Args:
        plaintext: The bytes to encrypt
        key: The device's 32-byte content key
        installation_id: The device's installation id, bound into the AAD
        nonce: A fixed nonce, for test vectors only -- random otherwise

    Returns:
        The base64 blob
    """
    if len(key) != CONTENT_KEY_BYTES:
        raise EnvelopeError(f"content key must be {CONTENT_KEY_BYTES} bytes, got {len(key)}")
    nonce = os.urandom(NONCE_BYTES) if nonce is None else nonce
    sealed = AESGCM(key).encrypt(nonce, plaintext, _aad(installation_id))
    return base64.b64encode(bytes([ENVELOPE_VERSION]) + nonce + sealed).decode("ascii")


def open_bytes(blob: str, key: bytes, installation_id: uuid.UUID) -> bytes:
    """
    Open a blob back into its plaintext bytes.

    Raises:
        EnvelopeError: The blob is malformed, of another version, or does not
            authenticate under this key and installation id
    """
    try:
        raw = base64.b64decode(blob, validate=True)
    except ValueError as exc:
        raise EnvelopeError("blob is not base64") from exc
    if len(raw) < 1 + NONCE_BYTES + 16 or raw[0] != ENVELOPE_VERSION:
        raise EnvelopeError("unknown envelope version or truncated blob")
    nonce, sealed = raw[1:1 + NONCE_BYTES], raw[1 + NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, sealed, _aad(installation_id))
    except Exception as exc:  # cryptography raises InvalidTag, a bare Exception subclass
        raise EnvelopeError("blob does not authenticate") from exc


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _clip(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def alert_payload(
    alert: Alert, *, badge: int, resolved: list[uuid.UUID],
) -> dict[str, Any]:
    """
    The plaintext of an alert's envelope.

    Args:
        alert: The alert being announced (need not be a stored row)
        badge: The badge this device should show, from alerts/badge.py
        resolved: Ids of alerts the device should withdraw if still shown

    Returns:
        The JSON-ready payload
    """

    def _id(value: uuid.UUID | None) -> str | None:
        return str(value) if value is not None else None

    return {
        "v": ENVELOPE_VERSION,
        "alert_id": str(alert.id),
        "kind": alert.kind,
        "title": _clip(alert.title, MAX_TITLE_CHARS) or "",
        "body": _clip(alert.body, MAX_BODY_CHARS),
        "account_id": _id(alert.account_id),
        "message_id": _id(alert.message_id),
        "folder_id": _id(alert.folder_id),
        "url": alert.url,
        "badge": badge,
        "resolved": [str(r) for r in resolved[:MAX_RESOLVED_IDS]],
    }


def seal_payload(payload: dict[str, Any], key: bytes, installation_id: uuid.UUID) -> str:
    """
    Seal a payload, dropping `resolved` entries from the end of the list
    (the oldest) until the blob fits MAX_BLOB_CHARS. What `resolved` carries is
    a convenience the next push repeats; the rest of the payload is the
    notification itself.
    """
    payload = dict(payload)
    resolved = list(payload.get("resolved", []))
    while True:
        payload["resolved"] = resolved
        blob = seal_bytes(_encode(payload), key, installation_id)
        if len(blob) <= MAX_BLOB_CHARS or not resolved:
            return blob
        resolved.pop()


def open_payload(blob: str, key: bytes, installation_id: uuid.UUID) -> dict[str, Any]:
    """Open a blob sealed by seal_payload back into its payload."""
    value = json.loads(open_bytes(blob, key, installation_id))
    if not isinstance(value, dict):
        raise EnvelopeError("envelope plaintext is not an object")
    return value
