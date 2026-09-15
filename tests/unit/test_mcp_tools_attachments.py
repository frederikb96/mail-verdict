"""
_decode_attachments and _check_attachment_limits -- the MCP attachment
path's base64 decoding and size caps, exercised without a database.
"""

from __future__ import annotations

import base64

import pytest

import mail_verdict.config.loader as loader
from mail_verdict.api.mcp_tools import _check_attachment_limits, _decode_attachments
from mail_verdict.config.loader import InfraConfig, get_config, reset_config
from tests.helpers.config_factory import make_config


@pytest.fixture()
def tiny_attachment_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small outbox.max_attachment_bytes -- proving the pre-decode size
    check fires needs only a handful of bytes over the limit, not a real
    multi-megabyte payload. A multiple of 3, so base64 needs no padding
    at exactly the limit -- the check estimates decoded size from the
    encoded length alone (len(b64) * 3 // 4), which padding makes an
    overestimate; a boundary test needs that estimate to be exact."""
    cfg_dict = make_config(
        outbox={"max_attachment_bytes": 9, "max_attachments_total_bytes": 100},
    )
    reset_config()
    monkeypatch.setattr(loader, "_CONFIG", cfg_dict)
    loader._config_instance = None
    get_config()


class TestDecodeAttachments:
    def test_decodes_a_plain_base64_attachment(self, test_config: InfraConfig) -> None:
        payload = base64.b64encode(b"hello").decode()
        result = _decode_attachments(
            [{"filename": "a.txt", "content_type": "text/plain", "data_base64": payload}],
        )
        assert result == [("a.txt", "text/plain", b"hello")]

    def test_accepts_a_null_content_type(self, test_config: InfraConfig) -> None:
        payload = base64.b64encode(b"hello").decode()
        result = _decode_attachments(
            [{"filename": "a.txt", "content_type": None, "data_base64": payload}],
        )
        assert result == [("a.txt", None, b"hello")]

    def test_strips_line_wrapping_before_decoding(self, test_config: InfraConfig) -> None:
        """Both coreutils' own `base64` and Python's encodebytes wrap
        their output at 76 columns -- a caller quoting either verbatim
        must not be refused as "invalid" for carrying the newlines that
        produced."""
        wrapped = base64.encodebytes(b"a" * 100).decode()
        assert "\n" in wrapped
        result = _decode_attachments(
            [{"filename": "a.bin", "content_type": None, "data_base64": wrapped}],
        )
        assert result == [("a.bin", None, b"a" * 100)]

    def test_a_missing_data_base64_is_an_error_dict_not_a_raise(
        self, test_config: InfraConfig,
    ) -> None:
        result = _decode_attachments([{"filename": "a.txt"}])
        assert isinstance(result, dict)
        assert "error" in result

    def test_invalid_base64_is_an_error_dict(self, test_config: InfraConfig) -> None:
        result = _decode_attachments(
            [{"filename": "a.txt", "data_base64": "not valid base64!!"}],
        )
        assert isinstance(result, dict)
        assert "error" in result

    def test_an_oversized_attachment_is_rejected_before_decoding(
        self, tiny_attachment_limits: None,
    ) -> None:
        payload = base64.b64encode(b"a" * 12).decode()  # 12 bytes > the 9-byte tiny limit
        result = _decode_attachments(
            [{"filename": "big.bin", "data_base64": payload}],
        )
        assert isinstance(result, dict)
        assert "size limit" in result["error"]

    def test_an_attachment_at_exactly_the_limit_is_accepted(
        self, tiny_attachment_limits: None,
    ) -> None:
        payload = base64.b64encode(b"a" * 9).decode()  # exactly the 9-byte tiny limit
        result = _decode_attachments(
            [{"filename": "ok.bin", "data_base64": payload}],
        )
        assert result == [("ok.bin", None, b"a" * 9)]


class TestCheckAttachmentLimits:
    def test_within_every_limit_is_accepted(self, tiny_attachment_limits: None) -> None:
        assert _check_attachment_limits([("a.bin", None, b"a" * 8)]) is None

    def test_total_across_attachments_over_the_cap_is_refused(
        self, tiny_attachment_limits: None,
    ) -> None:
        """Two attachments each within max_attachment_bytes can still
        together exceed max_attachments_total_bytes -- the per-item
        pre-decode check in _decode_attachments does not catch this."""
        attachments = [("a.bin", None, b"a" * 8) for _ in range(20)]  # 160 > 100-byte total cap
        result = _check_attachment_limits(attachments)
        assert result is not None
        assert "total size limit" in result["error"]
