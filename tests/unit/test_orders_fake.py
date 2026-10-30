"""
orders/fake.py: the keyword-only provider answers every shape the worker
now reads -- a kind, event dates, typed facts and code references -- and a
file is never answered "none".
"""

from __future__ import annotations

from mail_verdict.orders.fake import fake_decide, fake_write
from mail_verdict.orders.prompts import (
    WRITE_SCHEMA,
    PromptDocument,
    validate_decide_response,
    validate_write_response,
)


def _mail(subject: str, documents: list[PromptDocument] | None = None) -> dict[str, object]:
    return {
        "received_at": "2026-10-31T08:00:00+00:00", "from_addr": "Rail <rail@example.com>",
        "subject": subject, "body": "b", "documents": documents or [],
    }


def _document(text: str) -> PromptDocument:
    return PromptDocument(
        index=1, name="t.pdf", content_type="application/pdf", page_count=1,
        method="text_layer", text=text,
    )


def test_a_ticket_subject_gives_a_ticket_with_the_day_of_its_first_mail() -> None:
    answer = fake_write(mails=[_mail("Your ticket E551807712")], files=[])
    validate_write_response(answer)
    assert (answer["kind"], answer["event_start"], answer["event_end"]) == (
        "ticket", "2026-10-31", "2026-10-31",
    )
    assert answer["facts"][0]["kind"] == "order_number"
    assert set(answer) == set(WRITE_SCHEMA["properties"])


def test_other_subjects_give_an_order_or_other_without_event_dates() -> None:
    order = fake_write(mails=[_mail("Parcel shipped")], files=[])
    other = fake_write(mails=[_mail("Your subscription renewed")], files=[])
    assert (order["kind"], order["event_start"]) == ("order", "")
    assert other["kind"] == "other"


def test_every_code_marker_of_a_document_becomes_a_code_fact_once() -> None:
    document = _document('[code d1:b1 Aztec 290 bytes] x [code d1:b2 QR "1"] [code d1:b1 Aztec 2]')
    answer = fake_write(mails=[_mail("Ticket", [document])], files=[])
    refs = [f["code_ref"] for f in answer["facts"] if f["kind"] == "code"]
    assert refs == ["d1:b1", "d1:b2"]


def test_a_file_with_no_mail_still_gets_an_answer() -> None:
    file = PromptDocument(
        index=1, name="boarding.pdf", content_type="application/pdf", page_count=1,
        method="text_layer", text="x", added="2026-10-30",
    )
    answer = fake_write(mails=[], files=[file])
    validate_write_response(answer)
    assert (answer["merchant"], answer["subject"]) == ("Added file", "boarding.pdf")


def test_a_newsletter_is_none_for_a_mail_and_new_for_a_file() -> None:
    mail = fake_decide(subject="Our newsletter", candidates=[])
    file = fake_decide(subject="newsletter.pdf", candidates=[], allow_none=False)
    assert mail["target"] == "none" and file["target"] == "new"
    validate_decide_response(file, handles=[], allow_none=False)
