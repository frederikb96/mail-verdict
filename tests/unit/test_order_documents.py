"""
orders/documents.py: what the decide call shows of a new mail's attachments,
and how the write prompt numbers documents, rewrites their code markers and
fits under its ceiling by dropping middle mails.
"""

from __future__ import annotations

import uuid
from typing import Any

from mail_verdict.config.loader import InfraConfig
from mail_verdict.orders.documents import (
    DECIDE_ATTACHMENTS,
    ReadDocument,
    StoredDocument,
    assemble_write_input,
    attachment_haystack,
    decide_documents,
)
from mail_verdict.orders.extract.model import Barcode, ExtractedDocument

_ACCOUNT = uuid.uuid4()


def _read(name: str, text: str, *, barcodes: int = 0) -> ReadDocument:
    return ReadDocument(
        uuid.uuid4(), None,
        ExtractedDocument(
            filename=name, content_type="application/pdf", size_bytes=10, sha256="x",
            method="text_layer", page_count=1, text=text,
            barcodes=tuple(
                Barcode(i, "QR", b"x", "x", 1, None, None) for i in range(1, barcodes + 1)
            ),
        ),
    )


def _stored(msg_key: str | None, text: str, *, name: str = "t.pdf") -> StoredDocument:
    return StoredDocument(
        id=uuid.uuid4(), source="mail" if msg_key else "file",
        account_id=_ACCOUNT if msg_key else None, msg_key=msg_key, file_id=None,
        filename=name, content_type="application/pdf", page_count=1, method="text_layer",
        text=text, added_at=None,
    )


def _mail(key: str, body: str = "body") -> dict[str, Any]:
    return {
        "account_id": _ACCOUNT, "msg_key": key, "received_at": "2026-05-01T00:00:00+00:00",
        "from_addr": "a@b.com", "subject": key, "body": body,
    }


def test_the_decide_call_sees_an_excerpt_of_at_most_six_documents() -> None:
    reads = [_read(f"d{i}.pdf", "z " * 5_000) for i in range(8)]
    shown = decide_documents(reads)
    assert len(shown) == DECIDE_ATTACHMENTS == 6
    assert [d.index for d in shown] == [1, 2, 3, 4, 5, 6]
    assert all("characters omitted" in d.text and len(d.text) < 3_200 for d in shown)


def test_a_document_without_text_or_codes_is_not_shown() -> None:
    reads = [_read("logo.png", ""), _read("t.pdf", "Ticket")]
    assert [d.name for d in decide_documents(reads)] == ["t.pdf"]


def test_the_decide_excerpt_names_its_codes_by_prompt_number() -> None:
    shown = decide_documents([_read("a.pdf", "x"), _read("b.pdf", 'y\n[code b2 QR "55"]')])
    assert '[code d2:b2 QR "55"]' in shown[1].text


def test_the_number_search_reads_the_whole_text_of_every_attachment() -> None:
    reads = [_read("a.pdf", "first " + "pad " * 5_000 + "tail 00340434161094042557"),
             _read("b.pdf", "second")]
    haystack = attachment_haystack(reads)
    assert "00340434161094042557" in haystack and "second" in haystack


def test_documents_are_numbered_across_mails_then_files_and_codes_follow() -> None:
    first = _stored("m1", '[code b1 Aztec 290 bytes]\nTicket', name="a.pdf")
    second = _stored("m2", "[code b1 QR \"1\"] and [code b2 QR \"2\"]", name="b.pdf")
    uploaded = _stored(None, "[code b1 QR \"9\"]", name="c.png")
    result = assemble_write_input([_mail("m1"), _mail("m2")], [uploaded, second, first])

    assert [d.index for d in result.mails[0]["documents"]] == [1]
    assert [d.index for d in result.mails[1]["documents"]] == [2]
    assert [d.index for d in result.files] == [3]
    assert "[code d1:b1 Aztec 290 bytes]" in result.mails[0]["documents"][0].text
    assert result.code_map == {
        "d1:b1": (first.id, 1), "d2:b1": (second.id, 1), "d2:b2": (second.id, 2),
        "d3:b1": (uploaded.id, 1),
    }


def test_the_middle_mails_go_first_when_the_prompt_is_over_its_ceiling() -> None:
    mails = [_mail(f"m{i}", body="b" * 1_000) for i in range(6)]
    kept = assemble_write_input(mails, [], ceiling=3_500).mails
    assert kept[0]["msg_key"] == "m0" and kept[-1]["msg_key"] == "m5"
    assert 2 <= len(kept) < 6
    assert sum(len(m["body"]) for m in kept) <= 3_500


def test_a_dropped_mail_takes_its_documents_and_their_numbers_with_it() -> None:
    stored = [_stored(f"m{i}", f"[code b1 QR \"{i}\"]") for i in range(5)]
    result = assemble_write_input(
        [_mail(f"m{i}", body="b" * 1_000) for i in range(5)], stored, ceiling=3_000,
    )
    kept_keys = [m["msg_key"] for m in result.mails]
    assert len(kept_keys) < 5
    numbers = [d.index for m in result.mails for d in m["documents"]]
    assert numbers == list(range(1, len(numbers) + 1))
    assert set(result.code_map) == {f"d{n}:b1" for n in numbers}


def test_the_first_and_the_newest_mail_are_never_dropped() -> None:
    result = assemble_write_input(
        [_mail("old", "x" * 5_000), _mail("new", "y" * 5_000)], [], ceiling=10,
    )
    assert [m["msg_key"] for m in result.mails] == ["old", "new"]


def test_a_mail_document_without_text_stays_out_of_the_write_prompt_and_its_numbering() -> None:
    logo = _stored("m1", "", name="logo.png")
    ticket = _stored("m1", "[code b1 QR \"7\"]\nTicket", name="ticket.pdf")
    result = assemble_write_input([_mail("m1")], [logo, ticket])
    shown = result.mails[0]["documents"]
    assert [(d.index, d.name) for d in shown] == [(1, "ticket.pdf")]
    assert list(result.code_map) == ["d1:b1"]


def test_a_file_added_by_hand_is_listed_even_without_text() -> None:
    photo = _stored(None, "", name="photo.jpg")
    assert [d.name for d in assemble_write_input([_mail("m1")], [photo]).files] == ["photo.jpg"]


def test_a_failed_read_is_repeated_unless_the_file_was_too_big_to_read(
    test_config: InfraConfig,
) -> None:
    from mail_verdict.orders.documents import _needs_reading
    from mail_verdict.orders.extract import EXTRACTOR_VERSION

    limit = test_config.extraction.max_document_bytes
    assert _needs_reading(EXTRACTOR_VERSION, "failed", 1_000)
    assert _needs_reading(EXTRACTOR_VERSION, "failed", None)
    assert not _needs_reading(EXTRACTOR_VERSION, "failed", limit + 1)
    assert not _needs_reading(EXTRACTOR_VERSION, "text_layer", 1_000)
    assert not _needs_reading(EXTRACTOR_VERSION, "none", 1_000)
    assert _needs_reading(EXTRACTOR_VERSION - 1, "text_layer", 1_000)
