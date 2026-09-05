"""
The Settings page's information architecture: which categories the "AI &
automation" tabs offer, and the calendar invitations picker that used to
render one table column per calendar.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import wait_for_dav_account_active, wait_for_dav_collection
from tests.setup.dav_helpers import create_calendar, discover
from tests.ui.helpers import unique_email, wait_for, wait_for_account_active

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
    RADICALE_ALIAS,
    RADICALE_PORT,
)


class TestSettingsCategoriesUi:
    def test_ai_and_automation_offers_every_live_category_and_not_spam(
        self, page: Page, app_server: str,
    ) -> None:
        """The regression this guards: "spam" stopped being a settings
        category on the server (classification and rule actions moved into
        the pipeline), but the tab stayed in the interface -- opening it
        showed an unexplained empty box. "semantic" and "pipeline" are the
        opposite case: both are real server categories with no tab at all,
        reachable only by calling the API directly."""
        page.goto(f"{app_server}/settings")

        tablist = page.get_by_role("tablist")
        expect(tablist.get_by_role("tab", name="AI", exact=True)).to_be_visible(timeout=15_000)
        expect(tablist.get_by_role("tab", name="Semantic search", exact=True)).to_be_visible()
        expect(tablist.get_by_role("tab", name="Retry", exact=True)).to_be_visible()
        expect(tablist.get_by_role("tab", name="Pipeline", exact=True)).to_be_visible()

        with pytest.raises(AssertionError):
            expect(tablist.get_by_role("tab", name="Spam", exact=True)).to_be_visible(timeout=8_000)

        # Both new tabs render real, saved settings rather than the
        # "category is missing" fallback -- proves the category name here
        # actually matches what the server has under SettingCategory.
        # Field labels are humanized from the raw key ("provider" ->
        # "Provider"), which stays reachable as the label's title attribute.
        tablist.get_by_role("tab", name="Semantic search", exact=True).click()
        expect(page.get_by_text("Provider", exact=True)).to_be_visible(timeout=8_000)
        with pytest.raises(AssertionError):
            expect(page.get_by_text("didn't return settings", exact=False)).to_be_visible(
                timeout=8_000,
            )

        tablist.get_by_role("tab", name="Pipeline", exact=True).click()
        expect(page.get_by_text("Lease seconds", exact=True)).to_be_visible(timeout=8_000)


class TestProviderKeyFormUi:
    def test_a_key_can_be_entered_and_the_missing_encryption_key_error_is_named(
        self, page: Page, app_server: str,
    ) -> None:
        """There was no control anywhere in the application to set a
        provider key -- only the API directly. This proves the form
        exists and reaches PUT /api/settings/ai, and that a save refused
        for lacking ENCRYPTION_KEY (the test stack's own state, same as a
        fresh install) names that reason rather than failing silently."""
        page.goto(f"{app_server}/settings")

        # get_by_label matches by substring, and "Save Anthropic API key"
        # contains this label whole -- exact=True is what keeps the two apart.
        key_input = page.get_by_label("Anthropic API key", exact=True)
        expect(key_input).to_be_visible(timeout=15_000)
        save_button = page.get_by_role("button", name="Save Anthropic API key", exact=True)
        expect(save_button).to_be_disabled()

        key_input.fill("sk-test-not-a-real-key")
        expect(save_button).to_be_enabled()
        save_button.click()

        expect(
            page.get_by_text("ENCRYPTION_KEY must be configured", exact=False)
        ).to_be_visible(timeout=8_000)


@pytest.fixture(scope="module")
def radicale_base_url(radicale_endpoint: tuple[str, int]) -> str:
    host, port = radicale_endpoint
    return f"http://{host}:{port}/"


@pytest.fixture(scope="module")
def settings_calendar_owner(radicale_base_url: str) -> str:
    username = f"settings-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, radicale_base_url)
        create_calendar(client, principal, "personal", "Personal")
    return username


@pytest.fixture(scope="module")
def settings_dav_account(api_client: httpx.Client, settings_calendar_owner: str) -> dict[str, Any]:
    resp = api_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": settings_calendar_owner,
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_dav_account_active(api_client, account["id"])
    return account


@pytest.fixture(scope="module")
def settings_calendar(
    api_client: httpx.Client, settings_dav_account: dict[str, Any],
) -> dict[str, Any]:
    return wait_for_dav_collection(api_client, settings_dav_account["id"], "Personal")


@pytest.fixture(scope="module")
def settings_identity(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    """A real, active mail account plus an identity on it -- the row the
    calendar invitations picker renders one per."""
    email = unique_email("settings-links")
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

    resp = api_client.post("/api/identities", json={"account_id": account["id"], "address": email})
    assert resp.status_code == 201, resp.text
    identity = resp.json()
    identity["address"] = email
    return identity


class TestCalendarLinksPickerUi:
    def test_linking_a_calendar_through_the_chip_picker_persists(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        settings_identity: dict[str, Any],
        settings_calendar: dict[str, Any],
    ) -> None:
        """The regression this guards: the invitations panel rendered one
        table column per calendar, wide enough to overflow the screen well
        before thirty. A chip picker replaces it -- this proves linking a
        calendar through it actually reaches the server, not just the
        local row state."""
        page.goto(f"{app_server}/settings")

        identity_row = page.get_by_text(settings_identity["address"], exact=True).locator("..")
        identity_row.get_by_placeholder("Link a calendar...").click()
        page.get_by_role("option", name=settings_calendar["display_name"], exact=True).click()
        page.keyboard.press("Escape")

        # The linked calendar's name legitimately appears twice once linked
        # -- once as the chip, once as the receiving-select's own display
        # value that auto-picks the only linked calendar -- so a plain
        # get_by_text() here hits Playwright's strict-mode collision.
        # Scope to the chip specifically.
        chip = identity_row.locator('[data-slot="combobox-chip"]').get_by_text(
            settings_calendar["display_name"], exact=True,
        )
        expect(chip).to_be_visible(timeout=8_000)

        page.get_by_role("button", name="Save", exact=True).click()

        def _linked() -> dict[str, Any] | None:
            body = api_client.get("/api/calendar/links").json()
            row = next(
                (r for r in body["rows"] if r["identity_id"] == settings_identity["id"]), None,
            )
            if row and settings_calendar["id"] in row["calendar_ids"]:
                return row
            return None

        row = wait_for(_linked, description="Calendar link saved for the identity")
        assert row["receives_invitations_calendar_id"] == settings_calendar["id"]
