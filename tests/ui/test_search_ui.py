"""
Search page: the folder picker's default and its persistence, semantic
mode hiding the field toggles, and the virtualization law itself -- a
search returning far more matches than fit on screen must still mount
only a bounded number of rows.

The fixture account here is seeded directly into the mirror, not through
a real Dovecot account -- these tests never touch IMAP, only what the
search endpoint reads out of accounts/folders/messages, so a bare row is
enough and avoids the cost of a real sync for a mailbox nothing here ever
reads mail *from*.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, Uuid, insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.e2e.helpers import wait_for_dav_account_active, wait_for_dav_collection
from tests.setup.dav_helpers import create_addressbook, discover
from tests.ui.helpers import unique_email

from tests.setup.containers import RADICALE_ALIAS, RADICALE_PORT  # isort: skip

# A real GIF, not a placeholder string -- see test_mail_rendering_ui.py's
# own copy of this constant for why the avatar test needs actual image
# bytes rather than an arbitrary base64 string.
_TINY_GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="

_MATCH_COUNT = 1500
_MARKER = "scaletestmarker"


def _seed_scale_fixture(postgres_url: str, count: int) -> tuple[str, str]:
    """Bulk-insert a bare account, one folder, and `count` messages all
    matching _MARKER -- a single INSERT, the same shape
    tests/setup/large_mailbox.py uses, since delivering this many one at a
    time over LMTP would make the fixture itself the slow part of the
    test. Returns (account_id, folder_id) as strings."""

    async def _run() -> tuple[str, str]:
        engine = create_async_engine(postgres_url)
        account_id = uuid.uuid4()
        folder_id = uuid.uuid4()
        async with engine.begin() as conn:
            email = unique_email("search-scale")
            await conn.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, name, imap_host, imap_port, imap_user, imap_password) "
                    "VALUES (:id, :name, 'imap.example.com', 993, :email, "
                    "'\\x00' || convert_to('pw', 'UTF8'))"
                ),
                {"id": account_id, "name": email, "email": email},
            )
            await conn.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name) "
                    "VALUES (:id, :account_id, 'INBOX')"
                ),
                {"id": folder_id, "account_id": account_id},
            )
            # A second, empty folder -- so the picker has more than one to
            # offer. TestFolderPicker narrows to just this one and back.
            await conn.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name) "
                    "VALUES (:id, :account_id, 'Archive')"
                ),
                {"id": uuid.uuid4(), "account_id": account_id},
            )
            table = Table(
                "messages", MetaData(),
                Column("id", Uuid), Column("account_id", Uuid), Column("folder_id", Uuid),
                Column("imap_uid", BigInteger), Column("thread_id", Uuid),
                Column("message_id", Text),
                Column("subject", Text), Column("from_addr", Text),
                Column("received_at", DateTime(timezone=True)),
            )
            base_time = datetime(2026, 1, 1, tzinfo=UTC)
            rows = [
                {
                    "id": uuid.uuid4(), "account_id": account_id, "folder_id": folder_id,
                    "imap_uid": i + 1, "thread_id": uuid.uuid4(),
                    "message_id": f"<{uuid.uuid4()}@search-scale.example.com>",
                    "subject": f"{_MARKER} item {i}",
                    "from_addr": f"sender{i % 20}@example.com",
                    "received_at": base_time + timedelta(minutes=i),
                }
                for i in range(count)
            ]
            await conn.execute(insert(table), rows)
        await engine.dispose()
        return str(account_id), str(folder_id)

    # Not a plain asyncio.run() -- pytest-playwright's sync API bridges to
    # its own asyncio loop by making one appear "running" on the main
    # thread for the duration of the browser/page fixtures (see
    # tests/ui/conftest.py's app_server docstring), and asyncio.run()
    # refuses to nest inside a loop that is already running.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


@pytest.fixture(scope="module")
def scale_fixture(postgres_url: str) -> tuple[str, str]:
    return _seed_scale_fixture(postgres_url, _MATCH_COUNT)


class TestSearchVirtualization:
    def test_thousands_of_matches_mount_a_bounded_number_of_rows(
        self, page: Page, app_server: str, scale_fixture: tuple[str, str],
    ) -> None:
        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)

        rows = page.locator('[data-testid="search-result-row"]')
        expect(rows.first).to_be_visible(timeout=15_000)

        # Give the list a moment to settle at its natural mounted count
        # (past the debounce and the first page's own load), then assert
        # it stays far below the match count -- proving virtualization
        # rather than merely that results appeared at all.
        page.wait_for_timeout(1500)
        mounted = rows.count()
        assert 0 < mounted < 150, (
            f"mounted {mounted} rows for {_MATCH_COUNT} matches -- not virtualized"
        )


class TestFolderPicker:
    def test_defaults_to_all_folders_and_a_narrowed_change_persists_across_reload(
        self, page: Page, app_server: str, scale_fixture: tuple[str, str],
    ) -> None:
        page.goto(f"{app_server}/search")
        trigger = page.get_by_role("button", name="All folders", exact=True)
        expect(trigger).to_be_visible(timeout=15_000)

        trigger.click()
        page.get_by_role("checkbox", name="Archive", exact=True).uncheck()
        page.keyboard.press("Escape")
        expect(page.get_by_role("button", name="1 of 2 folders", exact=True)).to_be_visible()

        page.reload()
        expect(page.get_by_role("button", name="1 of 2 folders", exact=True)).to_be_visible(
            timeout=15_000
        )

        # Restore the default for any test running later in this module.
        page.get_by_role("button", name="1 of 2 folders", exact=True).click()
        page.get_by_text("Select all", exact=True).click()
        expect(page.get_by_role("button", name="All folders", exact=True)).to_be_visible()

    def test_the_last_folder_left_cannot_be_deselected(
        self, page: Page, app_server: str, scale_fixture: tuple[str, str],
    ) -> None:
        """An empty folder scope reads "unrestricted" on the wire (an
        absent query param), so unchecking the last folder must refuse --
        the same guard the field toggles already have. There is also no
        "Deselect all" button any more, since its only possible outcome is
        exactly that state."""
        page.goto(f"{app_server}/search")
        trigger = page.get_by_role("button", name="All folders", exact=True)
        expect(trigger).to_be_visible(timeout=15_000)
        trigger.click()

        expect(page.get_by_text("Deselect all", exact=True)).to_have_count(0)

        page.get_by_role("checkbox", name="Archive", exact=True).uncheck()
        inbox_checkbox = page.get_by_role("checkbox", name="INBOX", exact=True)
        expect(inbox_checkbox).to_be_checked()
        # .uncheck() asserts its own postcondition (unchecked afterward) and
        # raises when that doesn't hold -- exactly what refusing this click
        # produces, so a plain .click() is what proves the refusal rather
        # than failing the test on it.
        inbox_checkbox.click()

        # Refused: still checked, nothing collapsed to an empty scope.
        expect(inbox_checkbox).to_be_checked()

        # Restore the default for any test running later in this module.
        page.get_by_text("Select all", exact=True).click()
        page.keyboard.press("Escape")
        expect(page.get_by_role("button", name="All folders", exact=True)).to_be_visible()


class TestSemanticMode:
    def test_toggling_semantic_hides_the_field_toggles(
        self, page: Page, app_server: str, scale_fixture: tuple[str, str],
    ) -> None:
        page.goto(f"{app_server}/search")
        subject_toggle = page.get_by_role("button", name="Subject", exact=True)
        expect(subject_toggle).to_be_visible(timeout=15_000)

        page.get_by_role("switch", name="Semantic search").click()

        # to_have_count(0) alone would pass before React ever re-renders --
        # poll for the positive case timing out is what actually proves
        # the toggles are gone rather than merely not-yet-checked.
        with pytest.raises(AssertionError):
            expect(subject_toggle).to_be_visible(timeout=5_000)

        page.get_by_role("switch", name="Semantic search").click()
        expect(subject_toggle).to_be_visible(timeout=5_000)

    def test_semantic_search_with_no_provider_names_the_cause(
        self,
        page: Page,
        app_server: str,
        scale_fixture: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The provider key is resolved fresh on every call (env var
        fallback included), not cached at server startup -- so this can
        force the "no provider" case in-process even on a host whose own
        shell has a real OPENAI_API_KEY exported, which would otherwise be
        inherited here. The page must say so rather than show the
        ordinary empty state, which reads as "rephrase and try again"."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)

        page.get_by_role("switch", name="Semantic search").click()
        search_input.fill(_MARKER)

        expect(page.get_by_text("Semantic search is unavailable", exact=False)).to_be_visible(
            timeout=15_000
        )
        with pytest.raises(AssertionError):
            expect(page.get_by_text("No results found", exact=True)).to_be_visible(timeout=5_000)

        # Restore the default for any test running later in this module.
        page.get_by_role("switch", name="Semantic search").click()


class TestQueryPersistence:
    def test_a_query_survives_back_from_an_opened_result(
        self, page: Page, app_server: str, scale_fixture: tuple[str, str],
    ) -> None:
        """The folder scope and semantic mode already survive a reload;
        the typed query itself did not survive even a Back."""
        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)

        rows = page.locator('[data-testid="search-result-row"]')
        expect(rows.first).to_be_visible(timeout=15_000)
        rows.first.click()
        expect(page).to_have_url(f"{app_server}/")

        page.go_back()
        expect(page).to_have_url(f"{app_server}/search")
        expect(search_input).to_have_value(_MARKER)


_AVATAR_MARKER = "searchavatartestmarker"
_AVATAR_SENDER = "search-avatar-sender@example.com"


def _seed_avatar_fixture(postgres_url: str) -> tuple[str, str]:
    """One account, one folder, one message from `_AVATAR_SENDER` -- the
    single row this class's avatar test needs, seeded the same way
    `_seed_scale_fixture` above is, but for a single, specific sender
    rather than a spread of them."""

    async def _run() -> tuple[str, str]:
        engine = create_async_engine(postgres_url)
        account_id = uuid.uuid4()
        folder_id = uuid.uuid4()
        async with engine.begin() as conn:
            email = unique_email("search-avatar")
            await conn.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, name, imap_host, imap_port, imap_user, imap_password) "
                    "VALUES (:id, :name, 'imap.example.com', 993, :email, "
                    "'\\x00' || convert_to('pw', 'UTF8'))"
                ),
                {"id": account_id, "name": email, "email": email},
            )
            await conn.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name) "
                    "VALUES (:id, :account_id, 'INBOX')"
                ),
                {"id": folder_id, "account_id": account_id},
            )
            table = Table(
                "messages", MetaData(),
                Column("id", Uuid), Column("account_id", Uuid), Column("folder_id", Uuid),
                Column("imap_uid", BigInteger), Column("thread_id", Uuid),
                Column("message_id", Text),
                Column("subject", Text), Column("from_addr", Text),
                Column("received_at", DateTime(timezone=True)),
            )
            message_id = uuid.uuid4()
            await conn.execute(
                insert(table),
                [{
                    "id": message_id, "account_id": account_id, "folder_id": folder_id,
                    "imap_uid": 1, "thread_id": uuid.uuid4(),
                    "message_id": f"<{message_id}@search-avatar.example.com>",
                    "subject": f"{_AVATAR_MARKER} hello",
                    "from_addr": f"Avatar Sender <{_AVATAR_SENDER}>",
                    "received_at": datetime(2026, 1, 1, tzinfo=UTC),
                }],
            )
        await engine.dispose()
        return str(account_id), str(folder_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


@pytest.fixture(scope="module")
def avatar_fixture(postgres_url: str) -> tuple[str, str]:
    return _seed_avatar_fixture(postgres_url)


@pytest.fixture(scope="module")
def search_avatar_addressbook_owner(radicale_endpoint: tuple[str, int]) -> str:
    """An address book on the real Radicale server, owned by a fresh
    principal -- see test_contacts_ui.py's `ui_addressbook_owner`, the
    same pattern for the same reason."""
    host, port = radicale_endpoint
    base_url = f"http://{host}:{port}/"
    username = f"search-avatar-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, base_url)
        create_addressbook(client, principal, "search-avatar-book", "Search Avatar Book")
    return username


@pytest.fixture(scope="module")
def search_avatar_addressbook(
    api_client: httpx.Client, search_avatar_addressbook_owner: str,
) -> dict[str, Any]:
    resp = api_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": search_avatar_addressbook_owner,
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    dav_account = resp.json()
    wait_for_dav_account_active(api_client, dav_account["id"])
    return wait_for_dav_collection(api_client, dav_account["id"], "Search Avatar Book")


class TestSenderAvatar:
    """A search result row shows the same sender avatar the mail list and
    the reading pane do -- read from the same bulk photo index, not a
    request of its own (see search-result-row.tsx)."""

    def test_a_matching_contacts_embedded_photo_shows_in_a_search_result_row(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        avatar_fixture: tuple[str, str],
        search_avatar_addressbook: dict[str, Any],
    ) -> None:
        created = api_client.post(
            "/api/contacts",
            json={
                "addressbook_id": search_avatar_addressbook["id"],
                "summary": "Avatar Sender",
                "emails": [{"email": _AVATAR_SENDER}],
                "photo_data_url": _TINY_GIF,
            },
        )
        assert created.status_code == 201, created.text
        contact_id = created.json()["id"]

        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_AVATAR_MARKER)

        row = page.locator('[data-testid="search-result-row"]').first
        expect(row).to_be_visible(timeout=15_000)
        photo = row.locator('[data-slot="avatar-image"]')
        expect(photo).to_be_visible(timeout=10_000)
        assert photo.get_attribute("src") == f"/api/contacts/{contact_id}/photo"
