"""
The settings shell: category routes and the administrator's Models, Providers
and System categories, the calendar invitations picker, and the notification
folder picker.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import wait_for_dav_account_active, wait_for_dav_collection
from tests.setup.dav_helpers import create_calendar, discover
from tests.ui.helpers import (
    create_account,
    unique_email,
    wait_for,
    wait_for_account_active,
    wait_for_folder,
)

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
    RADICALE_ALIAS,
    RADICALE_PORT,
)


class TestAdminCategoriesUi:
    def test_models_lists_every_ai_task_as_one_row(self, page: Page, app_server: str) -> None:
        """Every AI task has a row in the one Models table, including the
        three orders tasks, and nothing offers "spam" as a task of its own:
        spam detection is a pipeline stage that calls the Spam verdicts
        model."""
        page.goto(f"{app_server}/admin/models")

        rows = page.get_by_test_id("model-row")
        expect(rows).to_have_count(6, timeout=15_000)
        for task in ("ai", "semantic", "assistant", "orders.decide", "orders.write", "orders.read"):
            expect(page.locator(f'[data-testid="model-row"][data-task="{task}"]')).to_be_visible()

    def test_processing_tuning_shows_the_queue_mechanics(
        self, page: Page, app_server: str,
    ) -> None:
        """The worker mechanics (pipeline, retry, orders queue keys) are
        real server categories with a form of their own under System, shut
        until asked for."""
        page.goto(f"{app_server}/admin/system")

        page.get_by_role("button", name="Show settings", exact=True).click()
        # Hand-authored label, not the mechanical humanizer's own "Lease
        # seconds" -- see SETTING_LABELS in setting-labels.ts.
        expect(page.get_by_text("Worker lease (seconds)", exact=True).first).to_be_visible(
            timeout=8_000,
        )


class TestProviderKeyFormUi:
    def test_a_key_can_be_entered_and_the_missing_encryption_key_error_is_named(
        self, page: Page, app_server: str,
    ) -> None:
        """There was no control anywhere in the application to set a
        provider key -- only the API directly. This proves the form
        exists and reaches PUT /api/settings/ai, and that a save refused
        for lacking ENCRYPTION_KEY (the test stack's own state, same as a
        fresh install) names that reason rather than failing silently."""
        page.goto(f"{app_server}/admin/providers")

        row = page.locator('[data-testid="provider-row"][data-provider="anthropic"]')
        row.get_by_role("button", name="Add key", exact=True).click(timeout=15_000)

        # The button's own name deliberately does not contain the input's --
        # a name that contains another makes every locator over this form a
        # strict-mode violation waiting for whoever writes the next test.
        key_input = page.get_by_label("Anthropic API key", exact=True)
        expect(key_input).to_be_visible(timeout=15_000)
        save_button = page.get_by_role("button", name="Store the Anthropic key", exact=True)
        expect(save_button).to_be_disabled()

        key_input.fill("sk-test-not-a-real-key")
        expect(save_button).to_be_enabled()
        save_button.click()

        expect(
            page.get_by_text("ENCRYPTION_KEY must be configured", exact=False)
        ).to_be_visible(timeout=8_000)


class TestDefaultCalendarSettingUi:
    def test_choosing_a_default_calendar_persists_and_the_editor_opens_on_it(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        settings_calendar: dict[str, Any],
    ) -> None:
        """The regression this guards: no default calendar existed
        anywhere -- not in settings, not in the manage dialog -- so a new
        event always landed in whichever calendar sorted first
        alphabetically. Proves both ends: the setting itself reaches the
        server, and the event editor's own initial Calendar value actually
        reads it back, not just the settings page displaying what it just
        wrote."""
        page.goto(f"{app_server}/settings/calendar")

        default_calendar_select = page.get_by_label("Default calendar")
        expect(default_calendar_select).to_be_visible(timeout=15_000)
        default_calendar_select.click()
        page.get_by_role("option", name=settings_calendar["display_name"], exact=True).click()
        page.get_by_test_id("settings-save-bar").get_by_role(
            "button", name="Save", exact=True,
        ).click()

        def _persisted() -> dict[str, Any] | None:
            data = api_client.get("/api/settings/calendar").json()
            return data if data.get("default_calendar_id") == settings_calendar["id"] else None

        wait_for(_persisted, description="Default calendar setting saved")

        page.goto(f"{app_server}/calendar")
        expect(page.get_by_role("checkbox", name="Personal")).to_be_visible(timeout=15_000)
        page.get_by_role("button", name="New event", exact=True).click()

        sheet = page.locator('[data-slot="sheet-content"]')
        expect(sheet).to_be_visible(timeout=15_000)
        calendar_select = sheet.locator('[data-slot="select-trigger"]').first
        # Not to_have_text: a Select trigger's textContent carries the chevron
        # glyph from its aria-hidden svg as well as the label, so an equality
        # assertion against the label alone can never pass here.
        expect(
            calendar_select.get_by_text(settings_calendar["display_name"], exact=True)
        ).to_be_visible(timeout=10_000)


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
        page.goto(f"{app_server}/settings/calendar")

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

    def test_the_invitation_controls_are_labelled(
        self,
        page: Page,
        app_server: str,
        settings_identity: dict[str, Any],
    ) -> None:
        """The regression this guards: the calendar picker and the "which
        one receives invitations" select were both unlabelled -- two
        unexplained controls per identity."""
        page.goto(f"{app_server}/settings/calendar")

        identity_row = page.get_by_text(settings_identity["address"], exact=True).locator("..")
        expect(identity_row.get_by_text("Linked calendars", exact=True)).to_be_visible(
            timeout=15_000,
        )
        expect(
            identity_row.get_by_text("New invitations go to", exact=True)
        ).to_be_visible()

    def test_edit_as_json_is_labelled_and_tucked_into_an_overflow(
        self, page: Page, app_server: str, settings_identity: dict[str, Any],
    ) -> None:
        """The regression this guards: an unlabelled `<>` icon button sat
        directly in the card header -- nothing said what it did."""
        page.goto(f"{app_server}/settings/calendar")

        expect(page.get_by_text(settings_identity["address"], exact=True)).to_be_visible(
            timeout=15_000,
        )
        with pytest.raises(AssertionError):
            expect(
                page.get_by_role("button", name="Edit as JSON", exact=True)
            ).to_be_visible(timeout=3_000)

        page.get_by_role("button", name="Calendar invitations options", exact=True).click()
        json_item = page.get_by_role("menuitem", name="Edit as JSON", exact=True)
        expect(json_item).to_be_visible(timeout=8_000)
        json_item.click()
        # The raw-JSON textarea's own class, not a generic role query.
        expect(page.locator("textarea.font-mono")).to_be_visible(timeout=8_000)


class TestSettingsShellUi:
    def test_a_category_link_opens_its_own_route(self, page: Page, app_server: str) -> None:
        """Settings is a list of categories with one open at a time, each at
        an address of its own, rather than one long scroll."""
        page.goto(f"{app_server}/settings/appearance")

        nav = page.get_by_role("navigation", name="Settings categories")
        expect(nav.get_by_role("link", name="Unified views")).to_be_visible(timeout=15_000)
        nav.get_by_role("link", name="Unified views").click()

        expect(page).to_have_url(f"{app_server}/settings/views")
        expect(page.get_by_role("heading", name="Unified views", exact=True)).to_be_visible()

    def test_the_bare_settings_address_opens_the_first_category(
        self, page: Page, app_server: str,
    ) -> None:
        page.goto(f"{app_server}/settings")

        expect(page).to_have_url(f"{app_server}/settings/appearance", timeout=15_000)

    def test_the_old_accounts_and_pipeline_addresses_still_open(
        self, page: Page, app_server: str,
    ) -> None:
        """/accounts and /pipeline are links people have saved: they open
        the Accounts category and the administrator's Processing category."""
        page.goto(f"{app_server}/accounts")
        expect(page.get_by_role("heading", name="Accounts", exact=True)).to_be_visible(
            timeout=15_000,
        )
        expect(page.get_by_role("link", name="Unified views")).to_be_visible()

        page.goto(f"{app_server}/pipeline")
        expect(page.get_by_role("heading", name="Processing", exact=True)).to_be_visible(
            timeout=15_000,
        )


class TestNotifyForFoldersRespectsVisibilityUi:
    def test_a_hidden_folder_is_absent_from_the_notify_list(
        self, page: Page, app_server: str, api_client: httpx.Client,
    ) -> None:
        """The regression this guards: the notify-folder picker listed
        every server folder, including ones the mail sidebar itself hides
        (an Exchange account's non-mail folders among them) -- the
        folder-visibility preference existed and simply wasn't reused
        here."""
        account = create_account(api_client, "settings-notify")
        suffix = uuid.uuid4().hex[:8]
        visible_name = f"NotifyVisible-{suffix}"
        hidden_name = f"NotifyHidden-{suffix}"

        resp = api_client.post(
            f"/api/accounts/{account['id']}/folders", json={"name": visible_name},
        )
        assert resp.status_code == 201, resp.text
        wait_for_folder(api_client, account["id"], resp.json()["imap_name"])

        resp = api_client.post(
            f"/api/accounts/{account['id']}/folders", json={"name": hidden_name},
        )
        assert resp.status_code == 201, resp.text
        hidden_folder = wait_for_folder(api_client, account["id"], resp.json()["imap_name"])

        prefs_resp = api_client.patch(
            f"/api/folders/{hidden_folder['id']}/prefs", json={"is_visible": False},
        )
        assert prefs_resp.status_code == 200, prefs_resp.text

        page.goto(f"{app_server}/settings/notifications")

        page.get_by_role("button", name="Customize", exact=True).click(timeout=15_000)
        picker = page.get_by_test_id("folder-picker")
        # The picker groups by account and collapses each group once there
        # is more than one account; open this test's own.
        group = picker.get_by_test_id("folder-picker-account").filter(
            has=page.get_by_text(account["name"], exact=True),
        )
        if group.get_by_test_id("folder-picker-folder").count() == 0:
            group.get_by_role("button", name=account["name"]).click()
        # Both names are unique to this test run, so neither can collide
        # with another account's own folders.
        expect(picker.get_by_text(visible_name, exact=True)).to_be_visible(timeout=15_000)
        with pytest.raises(AssertionError):
            expect(picker.get_by_text(hidden_name, exact=True)).to_be_visible(timeout=3_000)
