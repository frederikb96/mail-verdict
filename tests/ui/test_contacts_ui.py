"""
Contacts UI actions against a real Radicale server -- the contact editor's
own create path, which the e2e layer does not exercise since it never
drives the actual form.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.helpers import (
    unique_email,
    wait_for,
    wait_for_dav_account_active,
    wait_for_dav_collection,
)
from tests.setup.dav_helpers import create_addressbook, discover
from tests.ui.helpers import wait_for_account_active

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
    RADICALE_ALIAS,
    RADICALE_PORT,
)


@pytest.fixture(scope="module")
def radicale_base_url(radicale_endpoint: tuple[str, int]) -> str:
    host, port = radicale_endpoint
    return f"http://{host}:{port}/"


@pytest.fixture(scope="module")
def ui_addressbook_owner(radicale_base_url: str) -> str:
    """An address book on the real Radicale server, owned by a fresh
    principal -- the dav_account below points at it."""
    username = f"card-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, radicale_base_url)
        create_addressbook(client, principal, "friends", "Friends")
    return username


@pytest.fixture(scope="module")
def dav_account(api_client: httpx.Client, ui_addressbook_owner: str) -> dict[str, Any]:
    resp = api_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": ui_addressbook_owner,
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_dav_account_active(api_client, account["id"])
    return account


@pytest.fixture(scope="module")
def addressbook(api_client: httpx.Client, dav_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_dav_collection(api_client, dav_account["id"], "Friends")


@pytest.fixture(scope="module")
def ui_account(api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int]) -> dict[str, Any]:
    """An active mail account -- the compose dialog's recipient field is
    only reachable from a real account's compose flow."""
    _host, _imap_port, _lmtp_port = dovecot_endpoint
    email = unique_email("ui")
    resp = api_client.post(
        "/api/accounts",
        json={
            "name": email,
            "imap_host": DOVECOT_ALIAS,
            "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email,
            "imap_password": DOVECOT_PASSWORD,
            "smtp_host": MAILPIT_ALIAS,
            "smtp_port": MAILPIT_SMTP_PORT,
            "smtp_user": email,
            "smtp_password": "unused",  # Mailpit accepts any SMTP AUTH credentials
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_account_active(api_client, account["id"])
    account["email"] = email
    return account


@pytest.fixture(scope="module")
def seeded_contact(api_client: httpx.Client, addressbook: dict[str, Any]) -> dict[str, Any]:
    """A contact the compose autocomplete can actually find -- "ann" is a
    substring of both its name and its email."""
    resp = api_client.post(
        "/api/contacts",
        json={
            "addressbook_id": addressbook["id"],
            "summary": "Anna Testerson",
            "emails": [{"email": "anna.testerson@example.com"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _labeled_field(page: Page, label_text: str) -> Locator:
    """Several of contact-editor.tsx's repeatable fields (Phone, Address,
    Website) hold more than one input under one Label with no single
    htmlFor to point at, so get_by_label() cannot resolve them -- scoped
    instead to the one "grid gap-1.5" wrapper div that holds both the
    label and its own field(s), the only structural relationship there is
    between them."""
    return page.locator("div.grid").filter(has_text=label_text).locator("input, textarea").first


@pytest.fixture(scope="module")
def contact_with_year_less_birthday(
    api_client: httpx.Client, addressbook: dict[str, Any],
) -> dict[str, Any]:
    resp = api_client.post(
        "/api/contacts",
        json={
            "addressbook_id": addressbook["id"], "summary": f"No Year Birthday {uuid.uuid4()}",
            "emails": [{"email": f"{uuid.uuid4().hex[:8]}@example.com"}],
            "birthday": "--09-15",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture(scope="module")
def contact_with_no_birthday(
    api_client: httpx.Client, addressbook: dict[str, Any],
) -> dict[str, Any]:
    resp = api_client.post(
        "/api/contacts",
        json={
            "addressbook_id": addressbook["id"], "summary": f"No Birthday At All {uuid.uuid4()}",
            "emails": [{"email": f"{uuid.uuid4().hex[:8]}@example.com"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture(scope="module")
def contact_with_unparseable_birthday(
    api_client: httpx.Client, addressbook: dict[str, Any],
) -> dict[str, Any]:
    """`19900229` -- vCard's basic (undelimited) date format, which is
    legal and what several phone exporters write, but 1990 is not a leap
    year, so this is not a real calendar date at all. Neither the client
    nor the server rejects it at write time (a birthday is free text as
    far as this application is concerned); it stores and round-trips
    verbatim, and only rendering it needs to cope with it not being
    parseable into a real date."""
    resp = api_client.post(
        "/api/contacts",
        json={
            "addressbook_id": addressbook["id"],
            "summary": f"Unparseable Birthday {uuid.uuid4()}",
            "emails": [{"email": f"{uuid.uuid4().hex[:8]}@example.com"}],
            "birthday": "19900229",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestContactsUi:
    def test_creating_a_contact_from_the_editor_succeeds(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        addressbook: dict[str, Any],
    ) -> None:
        """The regression this guards: the default address book was read on
        first mount, before address books had loaded, so every create sent
        an empty addressbook_id and was rejected with the sheet left open
        and no feedback at all."""
        name = f"UI Contact Test {uuid.uuid4()}"
        email = f"{uuid.uuid4().hex[:8]}@example.com"

        page.goto(f"{app_server}/contacts")
        page.get_by_role("button", name="New contact", exact=True).click()

        name_input = page.get_by_label("Name", exact=True)
        expect(name_input).to_be_visible(timeout=15_000)
        name_input.fill(name)
        page.get_by_label("Email", exact=True).fill(email)
        page.get_by_role("button", name="Save", exact=True).click()

        expect(name_input).to_be_hidden(timeout=10_000)
        expect(page.get_by_text(name, exact=True)).to_be_visible(timeout=10_000)

        def _created() -> dict[str, Any] | None:
            resp = api_client.get("/api/contacts", params={"q": name})
            assert resp.status_code == 200, resp.text
            contacts = resp.json()["contacts"]
            return contacts[0] if contacts else None

        contact = wait_for(_created, description="Created contact synced back")
        assert contact["addressbook_id"] == addressbook["id"]

    def test_creating_a_contact_with_phone_year_less_birthday_and_categories(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        addressbook: dict[str, Any],
    ) -> None:
        """The editor's expanded field set -- phone with a type, a
        year-less birthday, and categories -- all reach the stored vCard.
        Address and photo share the same repeatable-row/upload machinery
        already proven for phone/email above and are covered end to end
        by the vcard.py unit and pg tests instead of a second UI pass."""
        name = f"UI Fields Test {uuid.uuid4()}"
        email = f"{uuid.uuid4().hex[:8]}@example.com"

        page.goto(f"{app_server}/contacts")
        page.get_by_role("button", name="New contact", exact=True).click()

        name_input = page.get_by_label("Name", exact=True)
        expect(name_input).to_be_visible(timeout=15_000)
        name_input.fill(name)
        page.get_by_label("Email", exact=True).fill(email)

        page.get_by_role("button", name="Add phone", exact=True).click()
        page.get_by_placeholder("Number", exact=True).fill("+491701234567")
        page.get_by_placeholder("Type", exact=True).fill("cell")

        page.locator("label").filter(has_text="I don't know the year").locator("input").check()
        page.locator("select").nth(0).select_option(label="September")
        page.locator("select").nth(1).select_option(value="15")

        page.get_by_label("Categories", exact=True).fill("Friend, Work")

        page.get_by_role("button", name="Save", exact=True).click()
        expect(name_input).to_be_hidden(timeout=10_000)

        # Creating leaves nothing selected -- open the new row to check
        # what actually got saved.
        expect(page.get_by_text(name, exact=True)).to_be_visible(timeout=10_000)
        page.get_by_text(name, exact=True).click()

        detail = page.locator(
            f"xpath=//h2[normalize-space(text())='{name}']"
            "/ancestor::div[contains(@class,'overflow-y-auto')][1]"
        )
        expect(detail.get_by_text("September 15", exact=True)).to_be_visible(timeout=10_000)
        expect(detail.get_by_text("Friend", exact=True)).to_be_visible()
        expect(detail.get_by_text("Work", exact=True)).to_be_visible()

        def _created() -> dict[str, Any] | None:
            resp = api_client.get("/api/contacts", params={"q": name})
            assert resp.status_code == 200, resp.text
            contacts = resp.json()["contacts"]
            return contacts[0] if contacts else None

        contact = wait_for(_created, description="Created contact synced back")
        assert contact["addressbook_id"] == addressbook["id"]
        assert contact["birthday"] == "--09-15"
        assert contact["phones"] == [{"number": "+491701234567", "type": "cell"}]
        assert set(contact["categories"]) == {"Friend", "Work"}

    def test_editing_a_contact_adds_details_and_search_finds_the_secondary_email(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        addressbook: dict[str, Any],
    ) -> None:
        """Neither of these is covered by the create test above: the
        editor's own edit path (organisation, title, a second email added
        to an existing contact) and search matching an address that isn't
        a contact's first."""
        name = f"UI Edit Test {uuid.uuid4()}"
        primary_email = f"{uuid.uuid4().hex[:8]}@example.com"
        secondary_email = f"{uuid.uuid4().hex[:8]}@example.com"
        resp = api_client.post(
            "/api/contacts",
            json={
                "addressbook_id": addressbook["id"], "summary": name,
                "emails": [{"email": primary_email}],
            },
        )
        assert resp.status_code == 201, resp.text
        contact = resp.json()

        page.goto(f"{app_server}/contacts")
        page.get_by_text(name, exact=True).click()

        # Icon-only, with no accessible name of its own -- see
        # contact-detail.tsx -- so located by the lucide icon class the
        # Edit control renders rather than a role/name pair it doesn't have.
        page.locator('button:has(svg.lucide-pencil)').click()

        organization_input = _labeled_field(page, "Organization")
        expect(organization_input).to_be_visible(timeout=15_000)
        organization_input.fill("Vex GmbH")
        _labeled_field(page, "Title").fill("Tester")

        page.get_by_role("button", name="Add email", exact=True).click()
        page.locator('input[type="email"]').nth(1).fill(secondary_email)

        page.get_by_role("button", name="Save", exact=True).click()
        expect(organization_input).to_be_hidden(timeout=10_000)

        # Scoped to the detail pane, not the page as a whole -- the
        # sidebar list row for this same contact shows its name and first
        # email as preview text right next to it, and an unscoped
        # get_by_text(primary_email) matches both.
        detail = page.locator(
            f"xpath=//h2[normalize-space(text())='{name}']"
            "/ancestor::div[contains(@class,'overflow-y-auto')][1]"
        )
        expect(detail.get_by_text("Tester at Vex GmbH", exact=True)).to_be_visible(timeout=10_000)
        expect(detail.get_by_text(primary_email, exact=True)).to_be_visible()
        expect(detail.get_by_text(secondary_email, exact=True)).to_be_visible()

        def _updated() -> dict[str, Any] | None:
            detail = api_client.get(f"/api/contacts/{contact['id']}").json()
            emails = {e["email"] for e in detail["emails"]}
            if detail["organization"] == "Vex GmbH" and secondary_email in emails:
                return detail
            return None

        wait_for(_updated, description="Edited contact synced back")

        page.get_by_placeholder("Search contacts").fill(secondary_email)
        # The detail pane still shows this same contact (untouched by the
        # search), so its own name heading is a second match for plain
        # text -- scoped to the list panel (contact-list.tsx's own
        # data-slot) rather than the page as a whole finds only the
        # search result row.
        contact_list = page.locator('[data-slot="contact-list"]')
        expect(contact_list.get_by_text(name, exact=True)).to_be_visible(timeout=10_000)

    def test_recipient_field_arrow_down_enter_commits_the_highlighted_suggestion(
        self,
        page: Page,
        app_server: str,
        ui_account: dict[str, Any],
        seeded_contact: dict[str, Any],
    ) -> None:
        """The regression this guards: Enter always committed the raw typed
        text as a chip, regardless of whether a suggestion was highlighted
        -- so arrow-down-then-enter on "ann" produced a literal "ann" chip
        instead of the highlighted anna.testerson@example.com, and
        parseAddressList happily accepted it, queuing a send that could
        only fail later."""
        page.goto(app_server)
        page.get_by_role("button", name="Compose").click()

        to_field = page.get_by_role("combobox", name="To")
        expect(to_field).to_be_visible(timeout=15_000)
        to_field.fill("ann")

        suggestion = page.get_by_role("option", name="anna.testerson@example.com")
        expect(suggestion).to_be_visible(timeout=10_000)
        to_field.press("ArrowDown")
        to_field.press("Enter")

        # A chip is the only thing that counts as "committed" -- the
        # suggestion option carries the same text and stays in the DOM
        # for a moment after a no-op Enter, so asserting on visible text
        # alone passes whether or not anything was actually selected. The
        # chip shows the contact's name when its search result is still
        # cached, its bare email otherwise -- "anna" matches either.
        chip = page.locator("[data-slot='combobox-chip']").filter(
            has_text=re.compile("anna", re.IGNORECASE)
        )
        expect(chip).to_be_visible(timeout=10_000)
        expect(page.get_by_role("option")).to_have_count(0)
        expect(page.get_by_text("ann", exact=True)).to_have_count(0)
        # Not `to_field` any more: once a chip exists, the field's own
        # accessible name (derived from its now-empty placeholder) is gone,
        # so the original name="To" locator no longer resolves to anything --
        # and a bare role="combobox" also matches the From account Select,
        # which carries the same role. The combobox's own input carries a
        # stable data-slot regardless of either.
        expect(page.locator('[data-slot="combobox-input"]')).to_have_value("")

    def test_recipient_field_rejects_unparseable_text_visibly(
        self, page: Page, app_server: str, ui_account: dict[str, Any],
    ) -> None:
        """Neither of the two silent failures: text that isn't an address
        is not turned into a chip that will only fail at send time, and it
        is not dropped without a trace either -- a toast names exactly what
        was rejected and why."""
        page.goto(app_server)
        page.get_by_role("button", name="Compose").click()

        to_field = page.get_by_role("combobox", name="To")
        expect(to_field).to_be_visible(timeout=15_000)
        to_field.fill("not-an-address")
        to_field.press("Enter")

        expect(
            page.get_by_text("Not a valid email address: not-an-address", exact=True)
        ).to_be_visible(timeout=10_000)
        expect(page.get_by_text("not-an-address", exact=True)).to_have_count(0)


class TestBirthdayCrash:
    """The regression this guards: opening a contact whose birthday is a
    partial or missing date threw "invalid time value" and took the whole
    page down -- date-fns' format() on new Date("--09-15") (year-less,
    RFC 6350's own shape for "we know the birthday, not the birth year").
    A card must render everything it can and stay quiet about what it
    cannot parse, never crash."""

    def test_a_year_less_birthday_renders_without_the_year(
        self,
        page: Page,
        app_server: str,
        contact_with_year_less_birthday: dict[str, Any],
    ) -> None:
        name = contact_with_year_less_birthday["summary"]
        page.goto(f"{app_server}/contacts")
        page.get_by_text(name, exact=True).click()

        detail = page.locator(
            f"xpath=//h2[normalize-space(text())='{name}']"
            "/ancestor::div[contains(@class,'overflow-y-auto')][1]"
        )
        expect(detail.get_by_text("September 15", exact=True)).to_be_visible(timeout=10_000)
        expect(page.get_by_text("Something went wrong", exact=False)).to_have_count(0)

    def test_a_contact_with_no_birthday_at_all_renders(
        self,
        page: Page,
        app_server: str,
        contact_with_no_birthday: dict[str, Any],
    ) -> None:
        name = contact_with_no_birthday["summary"]
        page.goto(f"{app_server}/contacts")
        page.get_by_text(name, exact=True).click()

        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible(timeout=10_000)
        expect(page.get_by_text("Something went wrong", exact=False)).to_have_count(0)


class TestUnparseableBirthday:
    """`19900229` is vCard's own basic date format, correctly stored and
    carried verbatim -- but it names a day that never existed (1990 is
    not a leap year), so it is not one of the three shapes the birthday
    parser understands. That must not mean it silently vanishes, and
    editing an unrelated field must not silently discard it either."""

    def test_the_detail_view_says_the_birthday_could_not_be_read(
        self,
        page: Page,
        app_server: str,
        contact_with_unparseable_birthday: dict[str, Any],
    ) -> None:
        name = contact_with_unparseable_birthday["summary"]
        page.goto(f"{app_server}/contacts")
        page.get_by_text(name, exact=True).click()

        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible(timeout=10_000)
        expect(page.get_by_text("Birthday could not be read", exact=True)).to_be_visible(
            timeout=10_000,
        )

    def test_saving_an_unrelated_edit_leaves_the_birthday_untouched(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        contact_with_unparseable_birthday: dict[str, Any],
    ) -> None:
        name = contact_with_unparseable_birthday["summary"]
        contact_id = contact_with_unparseable_birthday["id"]
        page.goto(f"{app_server}/contacts")
        page.get_by_text(name, exact=True).click()
        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible(timeout=10_000)

        page.get_by_role("button", name="Edit contact", exact=True).click()
        org_field = page.get_by_label("Organization", exact=True)
        expect(org_field).to_be_visible(timeout=10_000)
        org_field.fill("Acme Testing Co")
        page.get_by_role("button", name="Save", exact=True).click()

        def _organization_saved() -> bool | None:
            resp = api_client.get(f"/api/contacts/{contact_id}")
            assert resp.status_code == 200, resp.text
            return resp.json()["organization"] == "Acme Testing Co" or None

        wait_for(_organization_saved, description="Organization edit saved")

        refetched = api_client.get(f"/api/contacts/{contact_id}")
        assert refetched.status_code == 200, refetched.text
        assert refetched.json()["birthday"] == "19900229"


class TestUrlReflectsSelection:
    def test_opening_a_contact_updates_the_url_and_survives_the_back_button(
        self, page: Page, app_server: str, seeded_contact: dict[str, Any],
    ) -> None:
        page.goto(f"{app_server}/contacts")
        page.get_by_text(seeded_contact["summary"], exact=True).click()

        expect(page).to_have_url(re.compile(rf"[?&]id={seeded_contact['id']}"), timeout=10_000)

        page.go_back()
        expect(page).not_to_have_url(re.compile(rf"[?&]id={seeded_contact['id']}"), timeout=10_000)

    def test_a_direct_link_to_a_contact_opens_it(
        self, page: Page, app_server: str, seeded_contact: dict[str, Any],
    ) -> None:
        page.goto(f"{app_server}/contacts?id={seeded_contact['id']}")
        expect(
            page.get_by_role("heading", name=seeded_contact["summary"], exact=True)
        ).to_be_visible(timeout=15_000)


class TestMultiSelection:
    def test_shift_click_selects_a_range_and_bulk_delete_removes_it(
        self, page: Page, app_server: str, api_client: httpx.Client, addressbook: dict[str, Any],
    ) -> None:
        names = [f"UI Multiselect {uuid.uuid4()}" for _ in range(3)]
        for name in names:
            resp = api_client.post(
                "/api/contacts",
                json={
                    "addressbook_id": addressbook["id"], "summary": name,
                    "emails": [{"email": f"{uuid.uuid4().hex[:8]}@example.com"}],
                },
            )
            assert resp.status_code == 201, resp.text

        # The list sorts alphabetically by summary, not by creation order --
        # the shift-click range is the first-to-last row *as shown*, so the
        # endpoints have to be the alphabetically first/last of the three,
        # not names[0]/names[2] from this (UUID-ordered) list.
        shown_order = sorted(names)

        page.goto(f"{app_server}/contacts")
        first_checkbox = page.get_by_label(f"Select {shown_order[0]}")
        expect(first_checkbox).to_be_hidden(timeout=10_000)
        page.get_by_text(shown_order[0], exact=True).hover()
        first_checkbox.click()

        page.get_by_text(shown_order[2], exact=True).hover()
        page.get_by_label(f"Select {shown_order[2]}").click(modifiers=["Shift"])

        expect(page.get_by_text("3 selected", exact=True)).to_be_visible(timeout=10_000)

        page.get_by_role("button", name="Delete", exact=True).click()
        page.get_by_role("button", name="Delete permanently", exact=True).click()

        for name in names:
            expect(page.get_by_text(name, exact=True)).to_have_count(0)

        def _all_deleted() -> bool | None:
            resp = api_client.get("/api/contacts", params={"q": "UI Multiselect"})
            assert resp.status_code == 200, resp.text
            return all(c["summary"] not in names for c in resp.json()["contacts"]) or None

        wait_for(_all_deleted, description="Bulk-deleted contacts gone")

    def test_shift_click_range_selection_does_not_select_page_text(
        self, page: Page, app_server: str, api_client: httpx.Client, addressbook: dict[str, Any],
    ) -> None:
        """The browser's own shift-click-extends-a-selection behaviour is
        independent of this component's range-selection logic -- hovering
        a row's text before clicking its checkbox (exactly how a person
        finds the checkbox to click) plants a native selection anchor
        there, and a plain click leaves it inert, but a shift-click reads
        as "extend the selection to here" unless the row opts out."""
        names = [f"UI Multiselect Text {uuid.uuid4()}" for _ in range(2)]
        for name in names:
            resp = api_client.post(
                "/api/contacts",
                json={
                    "addressbook_id": addressbook["id"], "summary": name,
                    "emails": [{"email": f"{uuid.uuid4().hex[:8]}@example.com"}],
                },
            )
            assert resp.status_code == 201, resp.text

        shown_order = sorted(names)

        page.goto(f"{app_server}/contacts")
        first_checkbox = page.get_by_label(f"Select {shown_order[0]}")
        expect(first_checkbox).to_be_hidden(timeout=10_000)
        page.get_by_text(shown_order[0], exact=True).hover()
        first_checkbox.click()

        page.get_by_text(shown_order[1], exact=True).hover()
        page.get_by_label(f"Select {shown_order[1]}").click(modifiers=["Shift"])

        expect(page.get_by_text("2 selected", exact=True)).to_be_visible(timeout=10_000)
        selected_text = page.evaluate("window.getSelection().toString()")
        assert selected_text == "", f"shift-click also selected page text: {selected_text!r}"
