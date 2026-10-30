"""
orders/extract/truncate.py: the prompt budget for bodies and documents --
whole up to a limit, then head and tail around an explicit omission marker.
"""

from __future__ import annotations

from mail_verdict.orders.extract import truncate as t


def _numbered(lines: int) -> str:
    return "\n".join(f"line {n:05d} of the document" for n in range(lines))


def test_text_up_to_the_limit_is_kept_whole() -> None:
    text = "x " * (t.WHOLE_CHARS // 2)
    assert len(text) == t.WHOLE_CHARS
    assert t.truncate_text(text) == t.Truncated(text, 0)


def test_longer_text_keeps_head_and_tail_with_a_marker() -> None:
    text = _numbered(2_000)
    cut = t.truncate_text(text)
    assert cut.omitted > 0
    assert cut.text.startswith("line 00000 ")
    assert cut.text.endswith("line 01999 of the document")
    assert f"[... {cut.omitted} characters omitted ...]" in cut.text
    head, _, tail = cut.text.partition(f"\n[... {cut.omitted} characters omitted ...]\n")
    assert t.HEAD_CHARS - 250 < len(head) <= t.HEAD_CHARS
    assert t.TAIL_CHARS - 250 < len(tail) <= t.TAIL_CHARS


def test_the_omitted_count_is_exact() -> None:
    text = _numbered(2_000)
    cut = t.truncate_text(text)
    marker = f"\n[... {cut.omitted} characters omitted ...]\n"
    head, _, tail = cut.text.partition(marker)
    # What was dropped is the text between the kept head and tail, less the
    # line breaks trimmed at the seams.
    assert len(text) - cut.omitted - len(head) - len(tail) <= 2


def test_the_last_lines_of_a_document_survive() -> None:
    body = "boilerplate sentence. " * 3_000 + "\nTotal 232,13 EUR\nTicketcode 8H2K9"
    cut = t.truncate_text(body)
    assert "Ticketcode 8H2K9" in cut.text
    assert len(cut.text) < t.WHOLE_CHARS + 100


def test_terms_like_documents_get_the_small_budget() -> None:
    text = _numbered(2_000)
    plain = t.truncate_text(text)
    terms = t.truncate_text(text, terms_like=True)
    assert len(terms.text) < len(plain.text)
    assert len(terms.text) < t.TERMS_HEAD_CHARS + t.TERMS_TAIL_CHARS + 200
    assert terms.omitted > plain.omitted


def test_short_terms_are_kept_whole() -> None:
    text = "x " * 1_400
    assert t.truncate_text(text, terms_like=True).omitted == 0


def test_terms_are_recognised_by_name_or_opening_text() -> None:
    assert t.is_terms_like("AGB.pdf", "")
    assert t.is_terms_like("Widerrufsbelehrung.pdf", "")
    assert t.is_terms_like("doc.pdf", "Allgemeine Geschäftsbedingungen der Muster GmbH")
    assert t.is_terms_like("tos.html", "Terms of Service")
    assert not t.is_terms_like("Rechnung.pdf", "Rechnung Nr. 1")
    far = "Rechnung " + "x" * 700 + " AGB"
    assert not t.is_terms_like("Rechnung.pdf", far)
