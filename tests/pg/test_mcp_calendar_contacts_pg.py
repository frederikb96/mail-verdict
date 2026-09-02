"""
An MCP client lists calendars, creates, edits and deletes an
event, and searches and edits contacts -- against a real database, over
FastMCP's own in-memory Client rather than the underlying api/ functions
directly, since these tools are what an agent actually calls.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastmcp import Client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mcp_tools import mcp
from mail_verdict.database.connection import DatabaseConnection

_TARGETS = (
    "mail_verdict.api.calendar_events.get_db_connection",
    "mail_verdict.api.calendars.get_db_connection",
    "mail_verdict.api.contacts.get_db_connection",
)


async def _seed_calendar(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    dav_account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_accounts (id, name, url, username, password) "
            "VALUES (:id, :name, 'https://dav.example.com/', 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": dav_account_id, "name": f"dav-{dav_account_id}"},
    )
    collection_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_collections (id, account_id, kind, slug, display_name) "
            "VALUES (:id, :account_id, 'calendar', 'work', 'Work')"
        ),
        {"id": collection_id, "account_id": dav_account_id},
    )
    return dav_account_id, collection_id


async def _seed_addressbook(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    dav_account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_accounts (id, name, url, username, password) "
            "VALUES (:id, :name, 'https://dav.example.com/', 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": dav_account_id, "name": f"dav-{dav_account_id}"},
    )
    collection_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_collections (id, account_id, kind, slug, display_name) "
            "VALUES (:id, :account_id, 'addressbook', 'contacts', 'Contacts')"
        ),
        {"id": collection_id, "account_id": dav_account_id},
    )
    return dav_account_id, collection_id


@pytest_asyncio.fixture()
async def mcp_client(migrated_db: DatabaseConnection) -> AsyncIterator[Client]:
    """The MCP calendar/contact tools call directly into api/calendar_events.py,
    api/calendars.py and api/contacts.py -- each resolves its own database
    connection at call time, so all three need patching, not just
    api/mcp_tools.py's own."""
    patchers = [patch(target, return_value=migrated_db) for target in _TARGETS]
    for p in patchers:
        p.start()
    try:
        async with Client(mcp) as client:
            yield client
    finally:
        for p in patchers:
            p.stop()


class TestCalendarTools:
    @pytest.mark.asyncio
    async def test_list_calendars_returns_the_seeded_calendar(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _dav_account_id, collection_id = await _seed_calendar(session)
            await session.commit()

        result = await mcp_client.call_tool("list_calendars", {})
        calendars = result.data
        assert isinstance(calendars, list)
        assert any(c["id"] == str(collection_id) for c in calendars)

    @pytest.mark.asyncio
    async def test_create_update_and_delete_event_round_trips(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _dav_account_id, collection_id = await _seed_calendar(session)
            await session.commit()

        created = await mcp_client.call_tool(
            "create_event",
            {
                "calendar_id": str(collection_id), "summary": "Planning",
                "dtstart": "2026-09-10T10:00:00+00:00", "dtend": "2026-09-10T11:00:00+00:00",
                "rrule": "FREQ=WEEKLY;INTERVAL=2;COUNT=4",
            },
        )
        event = created.data
        assert "error" not in event, event
        assert event["summary"] == "Planning"
        object_id = event["object_id"]

        listed = await mcp_client.call_tool(
            "list_events", {"month": "2026-09", "calendar_ids": str(collection_id)},
        )
        assert any(e["summary"] == "Planning" for e in listed.data)

        updated = await mcp_client.call_tool(
            "update_event", {"event_id": object_id, "summary": "Planning (renamed)"},
        )
        assert updated.data["summary"] == "Planning (renamed)"
        assert updated.data["sequence"] == 1

        fetched = await mcp_client.call_tool("get_event", {"event_id": object_id})
        assert fetched.data["summary"] == "Planning (renamed)"

        deleted = await mcp_client.call_tool("delete_event", {"event_id": object_id})
        assert deleted.data == {"success": True}

        after_delete = await mcp_client.call_tool("get_event", {"event_id": object_id})
        assert "error" in after_delete.data

    @pytest.mark.asyncio
    async def test_create_event_error_surfaces_as_an_error_dict_not_an_exception(
        self, mcp_client: Client,
    ) -> None:
        """An unknown calendar_id is a 404 from calendar_events.create_event
        -- the MCP tool must turn that into {"error": ...}, matching every
        other tool's failure shape, rather than raising a ToolError."""
        result = await mcp_client.call_tool(
            "create_event",
            {
                "calendar_id": str(uuid.uuid4()), "summary": "Ghost",
                "dtstart": "2026-09-10T10:00:00+00:00", "dtend": "2026-09-10T11:00:00+00:00",
            },
        )
        assert "error" in result.data


class TestContactTools:
    @pytest.mark.asyncio
    async def test_create_update_and_delete_contact_round_trips(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _dav_account_id, addressbook_id = await _seed_addressbook(session)
            await session.commit()

        listed_books = await mcp_client.call_tool("list_addressbooks", {})
        assert any(a["id"] == str(addressbook_id) for a in listed_books.data)

        created = await mcp_client.call_tool(
            "create_contact",
            {
                "addressbook_id": str(addressbook_id), "summary": "Anna Mueller",
                "emails": [{"email": "anna@example.com", "type": "work"}],
            },
        )
        contact = created.data
        assert "error" not in contact, contact
        assert contact["summary"] == "Anna Mueller"
        contact_id = contact["id"]

        updated = await mcp_client.call_tool(
            "update_contact",
            {"contact_id": contact_id, "organization": "Example GmbH"},
        )
        assert updated.data["organization"] == "Example GmbH"
        # A field left unset is unchanged.
        assert updated.data["summary"] == "Anna Mueller"

        fetched = await mcp_client.call_tool("get_contact", {"contact_id": contact_id})
        assert fetched.data["organization"] == "Example GmbH"

        deleted = await mcp_client.call_tool("delete_contact", {"contact_id": contact_id})
        assert deleted.data == {"success": True}

        after_delete = await mcp_client.call_tool("get_contact", {"contact_id": contact_id})
        assert "error" in after_delete.data

    @pytest.mark.asyncio
    async def test_search_contacts_finds_a_match_by_email(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        """search_email_hits() filters on the parsed summary/emails
        columns, which PostIMAP writes back after a real outbound PUT
        lands against a live CardDAV server -- there is none here, so
        (matching test_contacts_api_pg.py's own search test) the row is
        seeded with those columns already populated rather than created
        through the tool and raced against PostIMAP's own timing."""
        async with migrated_db.session() as session:
            dav_account_id, addressbook_id = await _seed_addressbook(session)
            object_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO dav_objects "
                    "(id, account_id, collection_id, kind, data, summary, emails) "
                    "VALUES (:id, :account_id, :collection_id, 'addressbook', "
                    "'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Anna Mueller\r\n"
                    "EMAIL;TYPE=work:anna@example.com\r\nEND:VCARD\r\n', "
                    "'Anna Mueller', ARRAY['anna@example.com'])"
                ),
                {"id": object_id, "account_id": dav_account_id, "collection_id": addressbook_id},
            )
            await session.commit()

        found = await mcp_client.call_tool("search_contacts", {"q": "anna"})
        assert any(hit["email"] == "anna@example.com" for hit in found.data)
