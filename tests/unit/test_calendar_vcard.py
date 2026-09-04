"""calendar/vcard.py -- parsing and building VCARD bodies. No database,
no network: a round trip through vobject's real parser either works or
doesn't, which is exactly what these prove."""

from __future__ import annotations

import base64

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
        assert parsed.urls == ["https://example.com/anna"]
        assert parsed.notes == "Met at a conference"

    def test_missing_optional_fields_are_none_or_empty(self) -> None:
        bare = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:No Details\r\nN:;No Details;;;\r\nEND:VCARD\r\n"
        parsed = vcard.parse_contact(bare)
        assert parsed.summary == "No Details"
        assert parsed.emails == []
        assert parsed.organization is None
        assert parsed.birthday is None
        assert parsed.urls == []
        assert parsed.categories == []
        assert parsed.photo is None

    def test_year_less_birthday_is_kept_as_the_raw_rfc6350_shape(self) -> None:
        """A birthday with no year is a real vCard shape (`--MMDD` /
        `--MM-DD`), not a malformed one -- parsing must not reject it or
        try to coerce it into a full date server-side. Rendering it
        safely is the UI's job (see test_contacts_ui.py)."""
        card = (
            "BEGIN:VCARD\r\nVERSION:4.0\r\nFN:No Year\r\nBDAY:--09-15\r\nEND:VCARD\r\n"
        )
        parsed = vcard.parse_contact(card)
        assert parsed.birthday == "--09-15"

    def test_categories_parse_as_a_list(self) -> None:
        card = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Cats\r\n"
            "CATEGORIES:Friend,Work\r\nEND:VCARD\r\n"
        )
        parsed = vcard.parse_contact(card)
        assert parsed.categories == ["Friend", "Work"]

    def test_multiple_urls_all_parse(self) -> None:
        card = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Multi Url\r\n"
            "URL:https://a.example.com\r\nURL:https://b.example.com\r\nEND:VCARD\r\n"
        )
        parsed = vcard.parse_contact(card)
        assert parsed.urls == ["https://a.example.com", "https://b.example.com"]


class TestPhoto:
    def test_v3_base64_encoded_photo_is_embedded(self) -> None:
        payload = base64.b64encode(b"fake-jpeg-bytes").decode()
        card = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Photo\r\n"
            f"PHOTO;ENCODING=b;TYPE=JPEG:{payload}\r\nEND:VCARD\r\n"
        )
        parsed = vcard.parse_contact(card)
        assert parsed.photo is not None
        assert parsed.photo.kind == "embedded"
        assert parsed.photo.url == f"data:image/jpeg;base64,{payload}"

    def test_v4_inline_data_uri_photo_is_not_truncated_at_its_comma(self) -> None:
        """The regression this guards: vobject's default TEXT-value
        decoding for PHOTO treats the value as a comma-separated list and
        keeps only the first field, so `PHOTO:data:image/jpeg;base64,xxx`
        parsed through vobject directly loses the entire payload after
        the comma. `parse_contact` must read PHOTO its own way."""
        payload = base64.b64encode(b"another-fake-image-payload").decode()
        card = (
            "BEGIN:VCARD\r\nVERSION:4.0\r\nFN:Photo V4\r\n"
            f"PHOTO:data:image/jpeg;base64,{payload}\r\nEND:VCARD\r\n"
        )
        parsed = vcard.parse_contact(card)
        assert parsed.photo is not None
        assert parsed.photo.kind == "embedded"
        assert parsed.photo.url == f"data:image/jpeg;base64,{payload}"

    def test_remote_url_photo_is_never_treated_as_embedded(self) -> None:
        card = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Remote Photo\r\n"
            "PHOTO;VALUE=URI:https://example.com/photo.jpg\r\nEND:VCARD\r\n"
        )
        parsed = vcard.parse_contact(card)
        assert parsed.photo is not None
        assert parsed.photo.kind == "url"
        assert parsed.photo.url == "https://example.com/photo.jpg"

    def test_build_and_apply_round_trip_an_uploaded_photo(self) -> None:
        payload = base64.b64encode(b"uploaded-photo-bytes").decode()
        data_url = f"data:image/png;base64,{payload}"
        data = vcard.build_contact(
            summary="New Photo Contact",
            emails=[vcard.ContactEmail(email="p@example.com")],
            photo_data_url=data_url,
        )
        parsed = vcard.parse_contact(data)
        assert parsed.photo is not None
        assert parsed.photo.kind == "embedded"
        assert parsed.photo.url == data_url

        cleared = vcard.apply_contact_fields(data, photo_data_url="")
        assert vcard.parse_contact(cleared).photo is None

    def test_replacing_photo_removes_every_existing_photo_line(self) -> None:
        """A card can legally carry more than one PHOTO line -- some
        servers produce that shape. `card.photo` is vobject's singular
        accessor and only ever names the first, which would leave every
        other one behind rather than replaced."""
        first = base64.b64encode(b"first-photo").decode()
        second = base64.b64encode(b"second-photo").decode()
        card = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Multi Photo\r\n"
            f"PHOTO;ENCODING=b;TYPE=JPEG:{first}\r\n"
            f"PHOTO;ENCODING=b;TYPE=PNG:{second}\r\nEND:VCARD\r\n"
        )
        replacement = base64.b64encode(b"replacement-photo").decode()
        updated = vcard.apply_contact_fields(
            card, photo_data_url=f"data:image/png;base64,{replacement}",
        )
        photo_lines = [
            line for line in vcard._unfold_lines(updated) if line.upper().startswith("PHOTO")
        ]
        assert len(photo_lines) == 1


class TestCategories:
    def test_replacing_categories_removes_every_existing_categories_line(self) -> None:
        """Same shape as the PHOTO case above: `card.categories` only
        names the first CATEGORIES line, and a card can carry more than
        one."""
        card = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Multi Cats\r\n"
            "CATEGORIES:Friend\r\nCATEGORIES:Work\r\nEND:VCARD\r\n"
        )
        updated = vcard.apply_contact_fields(card, categories=["Only"])
        category_lines = [
            line for line in vcard._unfold_lines(updated)
            if line.upper().startswith("CATEGORIES")
        ]
        assert len(category_lines) == 1
        assert "Only" in category_lines[0]


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
