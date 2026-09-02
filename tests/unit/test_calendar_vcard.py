"""calendar/vcard.py -- parsing and building VCARD bodies. No database,
no network: a round trip through vobject's real parser either works or
doesn't, which is exactly what these prove."""

from __future__ import annotations

from mail_verdict.calendar import vcard

_SIMPLE_CARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:3.0\r\n"
    "UID:contact-1\r\n"
    "FN:Anna Mueller\r\n"
    "N:Mueller;Anna;;;\r\n"
    "EMAIL;TYPE=work:anna@example.com\r\n"
    "EMAIL;TYPE=home:anna.home@example.com\r\n"
    "ORG:Example GmbH\r\n"
    "TITLE:Engineer\r\n"
    "TEL;TYPE=cell:+491701234567\r\n"
    "ADR;TYPE=work:;;Main St 1;Berlin;;10115;Germany\r\n"
    "BDAY:1990-05-04\r\n"
    "URL:https://example.com/anna\r\n"
    "NOTE:Met at a conference\r\n"
    "END:VCARD\r\n"
)


class TestParsing:
    def test_parses_every_field(self) -> None:
        parsed = vcard.parse_contact(_SIMPLE_CARD)
        assert parsed.summary == "Anna Mueller"
        assert [e.email for e in parsed.emails] == [
            "anna@example.com", "anna.home@example.com",
        ]
        assert parsed.emails[0].type == "work"
        assert parsed.organization == "Example GmbH"
        assert parsed.title == "Engineer"
        assert parsed.phones[0].number == "+491701234567"
        assert "Berlin" in parsed.addresses[0].text
        assert parsed.birthday == "1990-05-04"
        assert parsed.url == "https://example.com/anna"
        assert parsed.notes == "Met at a conference"

    def test_missing_optional_fields_are_none_or_empty(self) -> None:
        bare = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:No Details\r\nN:;No Details;;;\r\nEND:VCARD\r\n"
        parsed = vcard.parse_contact(bare)
        assert parsed.summary == "No Details"
        assert parsed.emails == []
        assert parsed.organization is None
        assert parsed.birthday is None


class TestBuild:
    def test_build_contact_round_trips(self) -> None:
        data = vcard.build_contact(
            summary="New Contact",
            emails=[vcard.ContactEmail(email="new@example.com", type="work")],
            organization="Acme",
            phones=[vcard.ContactPhone(number="123456", type="cell")],
        )
        assert "BEGIN:VCARD" in data
        parsed = vcard.parse_contact(data)
        assert parsed.summary == "New Contact"
        assert parsed.emails[0].email == "new@example.com"
        assert parsed.organization == "Acme"
        assert parsed.phones[0].number == "123456"


class TestEdit:
    def test_apply_contact_fields_replaces_emails(self) -> None:
        updated = vcard.apply_contact_fields(
            _SIMPLE_CARD, emails=[vcard.ContactEmail(email="only@example.com")],
        )
        parsed = vcard.parse_contact(updated)
        assert [e.email for e in parsed.emails] == ["only@example.com"]
        # Untouched fields survive.
        assert parsed.summary == "Anna Mueller"
        assert parsed.organization == "Example GmbH"

    def test_apply_contact_fields_updates_summary_only(self) -> None:
        updated = vcard.apply_contact_fields(_SIMPLE_CARD, summary="Anna M.")
        parsed = vcard.parse_contact(updated)
        assert parsed.summary == "Anna M."
        assert len(parsed.emails) == 2

    def test_apply_contact_fields_can_clear_organization(self) -> None:
        updated = vcard.apply_contact_fields(_SIMPLE_CARD, organization="")
        parsed = vcard.parse_contact(updated)
        assert parsed.organization is None
