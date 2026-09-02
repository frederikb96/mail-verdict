"""
The actual write boundary: connected as postimap_app, not the Postgres owner.

Every other pg test connects as the database owner, so a write to a column
outside the consumer contract would pass there regardless of whether the
grant actually permits it. These tests run through the real grant instead:
a permitted write still works, and a write the contract does not grant --
even one that never reaches production code, a raw UPDATE on a read-only
column -- is refused by Postgres itself with permission denied.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import (
    DavAccount,
    DavObject,
    Folder,
    Message,
    Outbox,
)
from mail_verdict.postimap.actions import (
    acknowledge_all_dav_notifications,
    acknowledge_all_notifications,
    acknowledge_dav_notification,
    acknowledge_notification,
    create_account,
    create_collection,
    create_dav_account,
    create_folder,
    create_object,
    delete_account,
    delete_collection,
    delete_dav_account,
    delete_folder,
    delete_object,
    expunge,
    expunge_bulk,
    expunge_guarded,
    insert_outbox,
    move_message,
    move_message_bulk,
    move_message_guarded,
    move_object,
    move_to_trash,
    replace_object_data,
    set_flags,
    set_flags_bulk,
    set_flags_guarded,
    set_folder_idle,
    set_keywords,
    set_keywords_delta_guarded,
    update_account,
    update_collection,
    update_dav_account,
)


async def _seed_account_folder_message(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert a minimal account/folder/message chain via raw SQL, return their ids.

    Runs against migrated_db (the owner connection) -- the restricted role
    has no INSERT grant on any of these tables, which is exactly the
    boundary under test, so seeding can never go through it.
    """
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    message_id = uuid.uuid4()

    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, 'INBOX', NULL)"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id, 'Original subject')"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": uuid.uuid4(), "message_id": f"<{message_id}@example.com>",
        },
    )
    return account_id, folder_id, message_id


async def _seed_dav_account_collection_object(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert a minimal dav_accounts/dav_collections/dav_objects chain via
    raw SQL, return their ids. Runs against migrated_db (the owner
    connection) for the same reason as _seed_account_folder_message."""
    dav_account_id = uuid.uuid4()
    collection_id = uuid.uuid4()
    object_id = uuid.uuid4()

    await session.execute(
        text(
            "INSERT INTO dav_accounts (id, name, url, username, password) "
            "VALUES (:id, :name, 'https://dav.example.com/', 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": dav_account_id, "name": f"dav-{dav_account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO dav_collections (id, account_id, kind, slug, display_name) "
            "VALUES (:id, :account_id, 'calendar', 'work', 'Work')"
        ),
        {"id": collection_id, "account_id": dav_account_id},
    )
    await session.execute(
        text(
            "INSERT INTO dav_objects (id, account_id, collection_id, kind, data) "
            "VALUES (:id, :account_id, :collection_id, 'calendar', "
            "'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:original\r\nEND:VEVENT\r\nEND:VCALENDAR')"
        ),
        {"id": object_id, "account_id": dav_account_id, "collection_id": collection_id},
    )
    return dav_account_id, collection_id, object_id


@pytest.mark.asyncio
async def test_set_flags_succeeds_under_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """set_flags writes only is_seen -- a column postimap_app actually grants.

    The positive control: without this, a suite that only ever exercises
    the negative case could not tell "the role is scoped correctly" apart
    from "the role can write nothing at all".
    """
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_account_folder_message(session)
        await session.commit()

    async with restricted_db.session() as session:
        await set_flags(session, message_id, is_seen=True)
        await session.commit()

    async with migrated_db.session() as session:
        result = await session.execute(select(Message.is_seen).where(Message.id == message_id))
        assert result.scalar_one() is True


@pytest.mark.asyncio
async def test_writing_a_read_only_column_is_denied(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """subject is read-only per the contract -- the grant enforces it, not just the doc.

    This never goes through postimap/actions.py (nothing there writes
    subject); it proves the boundary exists at the database level even for
    a write our own code never attempts, which is the actual safety net if
    that ever changes by mistake.
    """
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_account_folder_message(session)
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with restricted_db.session() as session:
            await session.execute(
                update(Message).where(Message.id == message_id).values(subject="tampered")
            )
            await session.commit()

    assert "permission denied" in str(exc_info.value).lower()

    async with migrated_db.session() as session:
        result = await session.execute(select(Message.subject).where(Message.id == message_id))
        assert result.scalar_one() == "Original subject"


@pytest.mark.asyncio
async def test_insert_into_messages_is_denied(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """messages has no INSERT grant at all -- a row exists because it exists on IMAP.

    Postgres checks table-level INSERT privilege before evaluating any
    constraint, so this fails on the grant itself rather than on the
    made-up foreign keys below ever being validated.
    """
    with pytest.raises(DBAPIError) as exc_info:
        async with restricted_db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO messages (id, account_id, folder_id, thread_id, message_id) "
                    "VALUES (:id, :account_id, :folder_id, :thread_id, :msg_id)"
                ),
                {
                    "id": uuid.uuid4(), "account_id": uuid.uuid4(), "folder_id": uuid.uuid4(),
                    "thread_id": uuid.uuid4(), "msg_id": "<denied@example.com>",
                },
            )
            await session.commit()

    assert "permission denied" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_create_folder_succeeds_under_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """create_folder's Core INSERT names only account_id/imap_name -- exactly
    what postimap_app is granted -- and reads id back via RETURNING rather
    than sending a client-side one on the INSERT itself."""
    async with migrated_db.session() as session:
        account_id, _folder_id, _message_id = await _seed_account_folder_message(session)
        await session.commit()

    async with restricted_db.session() as session:
        new_folder_id = await create_folder(
            session, account_id=account_id, imap_name="Archive/2026",
        )
        await session.commit()

    async with migrated_db.session() as session:
        result = await session.execute(
            select(Folder.imap_name).where(Folder.id == new_folder_id)
        )
        assert result.scalar_one() == "Archive/2026"


@pytest.mark.asyncio
async def test_inserting_a_folder_id_directly_is_denied(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """id carries no INSERT grant on folders -- naming it explicitly, the way
    an ORM-constructed row with a client-side default would, is refused
    rather than silently accepted."""
    async with migrated_db.session() as session:
        account_id, _folder_id, _message_id = await _seed_account_folder_message(session)
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with restricted_db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name) "
                    "VALUES (:id, :account_id, 'Denied')"
                ),
                {"id": uuid.uuid4(), "account_id": account_id},
            )
            await session.commit()

    assert "permission denied" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_delete_folder_succeeds_under_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """delete_folder writes only deleted_at -- the one UPDATE grant on folders."""
    async with migrated_db.session() as session:
        account_id, _inbox_id, _message_id = await _seed_account_folder_message(session)
        folder_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use) "
                "VALUES (:id, :account_id, 'Archive', NULL)"
            ),
            {"id": folder_id, "account_id": account_id},
        )
        await session.commit()

    async with restricted_db.session() as session:
        await delete_folder(session, folder_id)
        await session.commit()

    async with migrated_db.session() as session:
        result = await session.execute(select(Folder.deleted_at).where(Folder.id == folder_id))
        assert result.scalar_one() is not None


@pytest.mark.asyncio
async def test_insert_outbox_with_replaces_message_id_succeeds_under_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """replaces_message_id is on outbox's column-level INSERT grant -- a draft
    edit must be writable through the real restricted role, not just the
    Postgres owner connection every other pg test uses."""
    async with migrated_db.session() as session:
        account_id, _inbox_id, message_id = await _seed_account_folder_message(session)
        await session.commit()

    async with restricted_db.session() as session:
        outbox = await insert_outbox(
            session, account_id=account_id, kind="draft",
            to_addrs=["them@example.com"], subject="Edited draft",
            body_text="Now finished.", replaces_message_id=message_id,
        )
        await session.commit()
        outbox_id = outbox.id

    async with migrated_db.session() as session:
        result = await session.execute(
            select(Outbox.replaces_message_id).where(Outbox.id == outbox_id)
        )
        assert result.scalar_one() == message_id


@pytest.mark.asyncio
async def test_create_dav_account_succeeds_under_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """create_dav_account writes exactly (id, name, url, username, password,
    is_active) -- the dav_accounts insert grant."""
    name = f"Nextcloud-{uuid.uuid4()}"
    async with restricted_db.session() as session:
        account = await create_dav_account(
            session, name=name, url="https://cloud.example.org/remote.php/dav/",
            username="alice", password="an-app-password",
        )
        await session.commit()
        account_id = account.id

    async with migrated_db.session() as session:
        result = await session.execute(select(DavAccount.name).where(DavAccount.id == account_id))
        assert result.scalar_one() == name


@pytest.mark.asyncio
async def test_creating_an_object_names_no_kind_column(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """dav_objects' insert grant is (id, account_id, collection_id, data) --
    kind carries none at all, a BEFORE INSERT trigger derives it from the
    collection. A create_object() that named kind explicitly would fail
    here rather than in a deployment."""
    async with migrated_db.session() as session:
        dav_account_id, collection_id, _object_id = await _seed_dav_account_collection_object(
            session,
        )
        await session.commit()

    async with restricted_db.session() as session:
        obj = await create_object(
            session, dav_account_id=dav_account_id, collection_id=collection_id,
            data="BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:new-event\r\nEND:VEVENT\r\nEND:VCALENDAR",
        )
        await session.commit()
        object_id = obj.id

    async with migrated_db.session() as session:
        result = await session.execute(select(DavObject.kind).where(DavObject.id == object_id))
        assert result.scalar_one() == "calendar"


@pytest.mark.asyncio
async def test_every_contract_write_survives_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """Sweep every write helper, not just the ones with their own test.

    A development database connects as an owner, and an owner bypasses
    grants entirely -- so a helper naming a column the consumer role may not
    write looks healthy there and fails only once deployed. That is how
    three PostIMAP-managed columns reached the outbox insert: they carried
    no default of any kind, so nothing marked them server-managed and the
    ORM named them anyway.

    Testing each helper individually leaves the next one uncovered. This
    asserts the property across the surface, so a helper added later is
    caught by an existing test rather than by a deployment.
    """
    async with migrated_db.session() as session:
        account_id, folder_id, message_id = await _seed_account_folder_message(session)
        # Separate objects for the destructive helpers, so removing one does
        # not decide whether a later write in the sweep reaches any rows --
        # an UPDATE matching nothing raises nothing, permission or not.
        _, doomed_folder_id, doomed_message_id = await _seed_account_folder_message(session)
        trash_folder_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use) "
                "VALUES (:id, :account_id, 'Trash', 'trash')"
            ),
            {"id": trash_folder_id, "account_id": account_id},
        )
        notification_id = (
            await session.execute(
                text(
                    "INSERT INTO sync_notifications (account_id, action) "
                    "VALUES (:account_id, 'set_flags') RETURNING id"
                ),
                {"account_id": account_id},
            )
        ).scalar_one()

        dav_account_id, collection_id, object_id = await _seed_dav_account_collection_object(
            session,
        )
        # A second collection under the same account, as a move target.
        move_target_collection_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO dav_collections (id, account_id, kind, slug) "
                "VALUES (:id, :account_id, 'calendar', 'personal')"
            ),
            {"id": move_target_collection_id, "account_id": dav_account_id},
        )
        # A separate account/collection/object for the DAV destructive
        # helpers, same reasoning as doomed_folder_id/doomed_message_id
        # above -- deleting one must not decide whether a later write in
        # the sweep reaches any rows.
        _, doomed_collection_id, doomed_object_id = await _seed_dav_account_collection_object(
            session,
        )
        dav_notification_id = (
            await session.execute(
                text(
                    "INSERT INTO dav_notifications (account_id, action) "
                    "VALUES (:account_id, 'put') RETURNING id"
                ),
                {"account_id": dav_account_id},
            )
        ).scalar_one()
        await session.commit()

    writes: list[tuple[str, Any]] = [
        ("insert_outbox", lambda s: insert_outbox(
            s, account_id=account_id, kind="draft",
            to_addrs=["sweep@example.com"], subject="sweep", body_text="x",
        )),
        ("insert_outbox_with_attachment", lambda s: insert_outbox(
            s, account_id=account_id, kind="draft",
            to_addrs=["sweep@example.com"], subject="sweep", body_text="x",
            attachments=[("a.txt", "text/plain", b"hello")],
        )),
        ("set_flags", lambda s: set_flags(s, message_id, is_seen=True)),
        ("set_keywords", lambda s: set_keywords(s, message_id, ["sweep"])),
        ("set_flags_bulk", lambda s: set_flags_bulk(s, [message_id], is_flagged=True)),
        ("move_message", lambda s: move_message(s, message_id, folder_id)),
        ("move_message_guarded", lambda s: move_message_guarded(
            s, message_id, folder_id, expected_folder_id=folder_id,
        )),
        ("set_flags_guarded", lambda s: set_flags_guarded(s, message_id, is_seen=True)),
        ("set_keywords_delta_guarded", lambda s: set_keywords_delta_guarded(
            s, message_id, add=["sweep2"], remove=[],
        )),
        ("set_folder_idle", lambda s: set_folder_idle(s, folder_id, requested=False)),
        ("update_account", lambda s: update_account(s, account_id, is_active=True)),
        ("expunge_guarded", lambda s: expunge_guarded(s, message_id)),
        ("create_account", lambda s: create_account(
            s, name=f"sweep-{uuid.uuid4()}", imap_host="imap.example.com", imap_port=993,
            imap_user="sweep@example.com", imap_password="pw", smtp_host=None,
            smtp_port=None, smtp_user=None, smtp_password=None, is_active=False,
        )),
        ("move_to_trash", lambda s: move_to_trash(s, doomed_message_id, trash_folder_id)),
        ("move_message_bulk", lambda s: move_message_bulk(
            s, [doomed_message_id], folder_id,
        )),
        ("expunge", lambda s: expunge(s, doomed_message_id)),
        ("expunge_bulk", lambda s: expunge_bulk(s, [doomed_message_id])),
        ("acknowledge_notification", lambda s: acknowledge_notification(s, notification_id)),
        ("acknowledge_all_notifications", lambda s: acknowledge_all_notifications(s, account_id)),
        ("create_folder", lambda s: create_folder(
            s, account_id=account_id, imap_name=f"Sweep-{uuid.uuid4().hex[:8]}",
        )),
        ("delete_folder", lambda s: delete_folder(s, doomed_folder_id)),
        ("delete_account", lambda s: delete_account(s, account_id)),
        ("create_dav_account", lambda s: create_dav_account(
            s, name=f"sweep-dav-{uuid.uuid4()}", url="https://dav.example.com/",
            username="sweep@example.com", password="pw",
        )),
        ("create_collection", lambda s: create_collection(
            s, dav_account_id=dav_account_id, kind="calendar",
            slug=f"sweep-{uuid.uuid4().hex[:8]}", display_name="Sweep",
        )),
        ("create_object", lambda s: create_object(
            s, dav_account_id=dav_account_id, collection_id=collection_id,
            data="BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:sweep\r\nEND:VEVENT\r\nEND:VCALENDAR",
        )),
        ("replace_object_data", lambda s: replace_object_data(
            s, object_id,
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:original\r\nSUMMARY:Edited\r\n"
            "END:VEVENT\r\nEND:VCALENDAR",
        )),
        ("move_object", lambda s: move_object(s, object_id, move_target_collection_id)),
        ("update_collection", lambda s: update_collection(
            s, collection_id, display_name="Renamed",
        )),
        ("update_dav_account", lambda s: update_dav_account(s, dav_account_id, is_active=True)),
        ("acknowledge_dav_notification", lambda s: acknowledge_dav_notification(
            s, dav_notification_id,
        )),
        ("acknowledge_all_dav_notifications", lambda s: acknowledge_all_dav_notifications(
            s, dav_account_id,
        )),
        ("delete_object", lambda s: delete_object(s, doomed_object_id)),
        ("delete_collection", lambda s: delete_collection(s, doomed_collection_id)),
        ("delete_dav_account", lambda s: delete_dav_account(s, dav_account_id)),
    ]

    _assert_sweep_covers_every_write_helper({name for name, _ in writes})

    denied: list[str] = []
    for name, write in writes:
        try:
            async with restricted_db.session() as session:
                await write(session)
                await session.commit()
        except ProgrammingError as exc:
            if "permission denied" in str(exc):
                denied.append(f"{name}: {str(exc).splitlines()[0][:90]}")
            else:
                raise

    assert not denied, (
        "these contract writes name a column the consumer role may not write, "
        "so they would fail in any deployment that is not connected as an "
        "owner:\n  " + "\n  ".join(denied)
    )


# Helpers in postimap/actions.py that issue no write of their own, so a grant
# has nothing to refuse. Everything else has to appear in the sweep above.
_NON_WRITING_HELPERS = frozenset({"format_credential", "force_reconnect"})


def _assert_sweep_covers_every_write_helper(covered: set[str]) -> None:
    """Fail if a helper in postimap/actions.py is absent from the sweep.

    The sweep is a hand-written list, so it claims a property it cannot
    keep on its own: a helper added afterwards is simply not in it, and
    nothing says so. Deriving the expected set from the module means the
    next helper is caught here rather than in a deployment.

    `force_reconnect` sends a NOTIFY over its own connection rather than
    writing a row, so it belongs to the command channel, not the write
    surface.
    """
    import inspect

    from mail_verdict.postimap import actions

    public = {
        name
        for name, obj in vars(actions).items()
        if not name.startswith("_")
        and (inspect.iscoroutinefunction(obj) or inspect.isfunction(obj))
        and getattr(obj, "__module__", None) == actions.__name__
    }
    # A label may name a variant of one helper ("insert_outbox_with_attachment").
    exercised = covered | {label.split("_with_")[0] for label in covered}
    missing = sorted(public - _NON_WRITING_HELPERS - exercised)
    assert not missing, (
        "these write helpers are not exercised under the restricted grant, so a "
        "column the consumer role may not write would reach a deployment "
        "unnoticed:\n  " + "\n  ".join(missing)
    )
