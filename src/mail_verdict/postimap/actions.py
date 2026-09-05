"""
The contract's write SQL -- every INSERT/UPDATE MailVerdict issues against
a PostIMAP-owned table lives here and only here.

Each function is close to literally the contract's own worked example.
Callers (api/, rules/, spam/) never construct this SQL themselves.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import any_, delete, insert, or_, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import (
    Account,
    DavAccount,
    DavCollection,
    DavNotification,
    DavObject,
    Folder,
    Message,
    Outbox,
    OutboxAttachment,
    SyncNotification,
)

_CREDENTIAL_FORMAT_PLAINTEXT = b"\x00"


def format_credential(plaintext: str) -> bytes:
    """
    Encode a plaintext password in the contract's consumer-write format.

    A consumer always writes format 0x00 (plaintext); PostIMAP is the only
    party that ever produces format 0x01 (its own AES-256-GCM encryption),
    rewriting the credential itself once the account starts syncing. The
    0x00 prefix is mandatory -- a bare-UTF-8 password has its first byte
    misread as the format byte and fails much later, inside the sync
    engine, not at insert time.

    Args:
        plaintext: The IMAP or SMTP password in plaintext

    Returns:
        The 0x00-prefixed bytes value to write to imap_password/smtp_password
    """
    return _CREDENTIAL_FORMAT_PLAINTEXT + plaintext.encode("utf-8")


async def create_account(
    session: AsyncSession,
    *,
    name: str,
    imap_host: str,
    imap_port: int,
    imap_user: str,
    imap_password: str,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_user: str | None = None,
    smtp_password: str | None = None,
    is_active: bool = True,
) -> Account:
    """
    Insert a new account row.

    PostIMAP detects the insert via postimap_events and starts syncing
    without a restart.

    Args:
        session: Active AsyncSession (caller commits)
        name: Unique display name
        imap_host: IMAP server hostname
        imap_port: IMAP server port
        imap_user: IMAP login username
        imap_password: IMAP password, plaintext (encoded here per contract)
        smtp_host: SMTP server hostname, optional (required for sending)
        smtp_port: SMTP server port, optional
        smtp_user: SMTP login username, optional
        smtp_password: SMTP password, plaintext, optional
        is_active: Whether PostIMAP should sync this account

    Returns:
        The inserted Account row (flushed, not yet committed)
    """
    account = Account(
        name=name,
        imap_host=imap_host,
        imap_port=imap_port,
        imap_user=imap_user,
        imap_password=format_credential(imap_password),
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=format_credential(smtp_password) if smtp_password else None,
        is_active=is_active,
    )
    session.add(account)
    await session.flush()
    await session.refresh(account)
    return account


async def update_account(
    session: AsyncSession,
    account_id: uuid.UUID,
    **fields: Any,
) -> None:
    """
    Update account fields, encoding imap_password/smtp_password if present.

    Args:
        session: Active AsyncSession (caller commits)
        account_id: Account to update
        **fields: Any writable Account column; imap_password and
            smtp_password are given as plaintext strings and encoded here
    """
    values = dict(fields)
    if values.get("imap_password"):
        values["imap_password"] = format_credential(values["imap_password"])
    if values.get("smtp_password"):
        values["smtp_password"] = format_credential(values["smtp_password"])
    if not values:
        return
    await session.execute(update(Account).where(Account.id == account_id).values(**values))


async def delete_account(session: AsyncSession, account_id: uuid.UUID) -> None:
    """
    Permanently delete an account and its entire mirrored mailbox.

    Available from PostIMAP service_version 1.0.1 onward -- gate the call
    site on postimap.contract.supports_account_delete() first, since an
    older PostIMAP has no DELETE grant on accounts and this fails with
    permission denied rather than a friendly error.

    Irreversible: folders, messages, attachments, sync_state, sync_audit,
    sync_queue and outbox all cascade via ON DELETE CASCADE against this
    row -- there is nothing else to delete. Nothing on the IMAP server
    itself is touched; re-adding the account re-syncs it from scratch.

    Args:
        session: Active AsyncSession (caller commits)
        account_id: Account to delete
    """
    await session.execute(delete(Account).where(Account.id == account_id))


async def force_reconnect(db: DatabaseConnection, account_id: uuid.UUID) -> None:
    """
    Bounce an account's sync connection by toggling is_active off then on.

    A credential rewritten on an account that is already running is not
    re-encrypted, and not used to reconnect, until that account restarts.
    Call this after updating imap_password/smtp_password on an account
    that was active, or the user sees no error and nothing changes --
    which reads as the app silently ignoring the new password. The two
    phases are separate committed transactions (not one UPDATE toggling
    back and forth) so PostIMAP actually observes both transitions.

    Args:
        db: Database connection (each phase gets its own committed session)
        account_id: Account to bounce
    """
    async with db.session() as session:
        await session.execute(
            update(Account).where(Account.id == account_id).values(is_active=False)
        )
    async with db.session() as session:
        await session.execute(
            update(Account).where(Account.id == account_id).values(is_active=True)
        )


async def force_reconnect_dav_account(db: DatabaseConnection, dav_account_id: uuid.UUID) -> None:
    """
    Bounce a DAV account's sync connection by toggling is_active off then
    on -- the DavAccount counterpart of force_reconnect() above.

    A credential rewritten on an account that is already running is not
    re-encrypted, and not used to reconnect, until that account restarts.
    Call this after updating password on a DAV account that was active,
    or the user sees no error and nothing changes -- which reads as the
    app silently ignoring the corrected password. The two phases are
    separate committed transactions (not one UPDATE toggling back and
    forth) so PostIMAP actually observes both transitions.

    Args:
        db: Database connection (each phase gets its own committed session)
        dav_account_id: DAV account to bounce
    """
    async with db.session() as session:
        await session.execute(
            update(DavAccount).where(DavAccount.id == dav_account_id).values(is_active=False)
        )
    async with db.session() as session:
        await session.execute(
            update(DavAccount).where(DavAccount.id == dav_account_id).values(is_active=True)
        )


async def set_flags(
    session: AsyncSession,
    message_id: uuid.UUID,
    **flags: bool,
) -> int:
    """
    Update one or more IMAP-mapped flags on a message.

    Args:
        session: Active AsyncSession (caller commits)
        message_id: Message to update
        **flags: Any of is_seen, is_flagged, is_answered, is_draft, is_deleted

    Returns:
        The number of rows actually updated (0 or 1) -- a caller reporting
        success on a write that touched nothing is the same "recorded as
        having worked" bug the pipeline's guarded effects exist to avoid.
    """
    if not flags:
        return 0
    result = await session.execute(
        update(Message).where(Message.id == message_id).values(**flags)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def set_keywords(
    session: AsyncSession,
    message_id: uuid.UUID,
    keywords: list[str],
) -> int:
    """
    Replace a message's custom IMAP keywords/labels.

    Args:
        session: Active AsyncSession (caller commits)
        message_id: Message to update
        keywords: Full replacement keyword list

    Returns:
        The number of rows actually updated (0 or 1)
    """
    result = await session.execute(
        update(Message).where(Message.id == message_id).values(keywords=keywords)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def move_message(
    session: AsyncSession,
    message_id: uuid.UUID,
    target_folder_id: uuid.UUID,
) -> int:
    """
    Move a message to a different folder.

    Setting imap_uid to NULL alongside the new folder_id is what makes this
    optimistic: PostIMAP writes the real UID back once the IMAP MOVE
    succeeds, and NULL never collides with another pending move under the
    UNIQUE(folder_id, imap_uid) constraint -- no sentinel value needed.
    imap_uid IS NULL is itself the "move pending" signal, surfaced in the
    API as pending_sync.

    A message already in target_folder_id is left untouched rather than
    clearing imap_uid for a folder change that will never happen -- without
    this, marking mail already in Junk as spam again (or the same shape for
    archive/trash/not_spam/an explicit move onto the current folder) writes
    imap_uid=NULL with folder_id unchanged, PostIMAP's move trigger only
    enqueues a sync when folder_id actually changed so nothing ever clears
    it, and the row spins as pending_sync forever.

    Args:
        session: Active AsyncSession (caller commits)
        message_id: Message to move
        target_folder_id: Destination folder

    Returns:
        The number of rows actually updated -- 0 either if the message is
        gone or if it was already in target_folder_id (a no-op, not a
        failure)
    """
    result = await session.execute(
        update(Message)
        .where(Message.id == message_id, Message.folder_id != target_folder_id)
        .values(folder_id=target_folder_id, imap_uid=None)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def move_to_trash(
    session: AsyncSession,
    message_id: uuid.UUID,
    trash_folder_id: uuid.UUID,
) -> int:
    """
    Move a message to the trash folder -- the default UI "delete".

    Distinct from expunge(): this is reversible, the message row and its
    IMAP presence both survive, just relocated.

    Args:
        session: Active AsyncSession (caller commits)
        message_id: Message to trash
        trash_folder_id: The account's trash-special-use folder

    Returns:
        The number of rows actually updated (0 or 1)
    """
    return await move_message(session, message_id, trash_folder_id)


async def expunge(session: AsyncSession, message_id: uuid.UUID) -> int:
    """
    Permanently remove a message -- enqueues an IMAP EXPUNGE.

    The row survives in Postgres (for audit/undo) with expunged_at set; it
    drops out of folder counts immediately. Distinct from the is_deleted
    \\Deleted flag, which only marks a message for deletion without
    removing it.

    Args:
        session: Active AsyncSession (caller commits)
        message_id: Message to expunge

    Returns:
        The number of rows actually updated (0 or 1)
    """
    result = await session.execute(
        update(Message).where(Message.id == message_id).values(expunged_at=text("now()"))
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def set_flags_bulk(
    session: AsyncSession,
    message_ids: list[uuid.UUID],
    **flags: bool,
) -> int:
    """
    Update one or more IMAP-mapped flags on many messages at once.

    Same contract columns as set_flags(), batched into a single UPDATE
    matched with `= ANY(:ids)` rather than an `IN (...)` list -- an IN
    clause binds one parameter per id, and asyncpg refuses a statement
    over 32767 parameters; ANY binds the whole id list as a single array
    parameter, so this scales to any folder size.

    Also filtered to rows where at least one named flag actually differs
    from its requested value -- without this, re-marking an already-read
    folder read reports every row as affected, and `affected_count` is
    user-facing (the bulk-action toast). PostIMAP's own triggers already
    gate the queue insert and the NOTIFY on `OLD IS DISTINCT FROM NEW`, so
    this guard only changes what is reported, not what would otherwise be
    written unsafely.

    Args:
        session: Active AsyncSession (caller commits)
        message_ids: Messages to update
        **flags: Any of is_seen, is_flagged, is_answered, is_draft, is_deleted

    Returns:
        The number of rows actually updated -- may be fewer than
        `len(message_ids)` if some had already been expunged, no longer
        matched, or already carried every named flag at its requested value.
    """
    if not flags or not message_ids:
        return 0
    actually_different = or_(
        *(getattr(Message, column).is_not(value) for column, value in flags.items())
    )
    result = await session.execute(
        update(Message)
        .where(Message.id == any_(message_ids), actually_different)  # type: ignore[arg-type]
        .values(**flags)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def move_message_bulk(
    session: AsyncSession,
    message_ids: list[uuid.UUID],
    target_folder_id: uuid.UUID,
) -> int:
    """
    Move many messages to a different folder at once.

    Same optimistic folder_id + imap_uid=NULL shape as move_message(),
    batched into a single UPDATE matched with `= ANY(:ids)` -- see
    set_flags_bulk() for why this is not an IN (...) list.

    Args:
        session: Active AsyncSession (caller commits)
        message_ids: Messages to move
        target_folder_id: Destination folder

    Returns:
        The number of rows actually updated -- may be fewer than
        `len(message_ids)`, since a message already in target_folder_id is
        left untouched rather than clearing imap_uid for nothing (see
        move_message())
    """
    if not message_ids:
        return 0
    result = await session.execute(
        update(Message)
        .where(
            Message.id == any_(message_ids),  # type: ignore[arg-type]
            Message.folder_id != target_folder_id,
        )
        .values(folder_id=target_folder_id, imap_uid=None)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def expunge_bulk(session: AsyncSession, message_ids: list[uuid.UUID]) -> int:
    """
    Permanently remove many messages at once -- see expunge() and
    set_flags_bulk() for the ANY-array batching.

    Args:
        session: Active AsyncSession (caller commits)
        message_ids: Messages to expunge

    Returns:
        The number of rows actually updated -- may be fewer than
        `len(message_ids)`
    """
    if not message_ids:
        return 0
    result = await session.execute(
        update(Message).where(Message.id == any_(message_ids)).values(expunged_at=text("now()"))  # type: ignore[arg-type]
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def create_folder(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    imap_name: str,
) -> uuid.UUID:
    """
    Insert a folder row -- enqueues an IMAP CREATE.

    IMAP has no parent-folder concept, so imap_name must already be the
    full separator-joined path; building it from a parent folder plus a
    separator read off an existing row of the account is the caller's job
    (see the contract's "Creating a folder" worked example).

    id carries no INSERT grant on folders, so this issues a Core INSERT
    naming only account_id/imap_name and reads the database-generated id
    back via RETURNING, rather than letting an ORM-constructed row send a
    client-side id the grant would reject.

    Available from PostIMAP service_version 1.3.0 onward -- gate the call
    site on postimap.contract.supports_folder_crud() first.

    Args:
        session: Active AsyncSession (caller commits)
        account_id: Account the folder belongs to
        imap_name: Full IMAP mailbox path, separator-joined

    Returns:
        The database-generated id of the new folder row
    """
    result = await session.execute(
        insert(Folder).values(account_id=account_id, imap_name=imap_name).returning(Folder.id)
    )
    return cast(uuid.UUID, result.scalar_one())


async def delete_folder(session: AsyncSession, folder_id: uuid.UUID) -> None:
    """
    Delete a folder -- enqueues an IMAP DELETE.

    This destroys every message in the folder on the mail server,
    irreversibly; there is no undo. A delete the server refuses (deleting
    INBOX, for example) leaves the folder and its messages untouched and
    surfaces as a sync_error event rather than as a synchronous failure of
    this statement.

    Available from PostIMAP service_version 1.3.0 onward -- gate the call
    site on postimap.contract.supports_folder_crud() first.

    Args:
        session: Active AsyncSession (caller commits)
        folder_id: Folder to delete
    """
    await session.execute(
        update(Folder).where(Folder.id == folder_id).values(deleted_at=text("now()"))
    )


async def set_folder_idle(
    session: AsyncSession, folder_id: uuid.UUID, *, requested: bool
) -> None:
    """
    Ask PostIMAP to watch this folder for changes, or stop watching it.

    A watched folder holds its own IMAP connection open, so changes arrive
    in seconds instead of on the sync interval. PostIMAP answers on
    folders.idle_status rather than through this statement -- a server that
    does not support it, or a watch that exhausts its reconnection
    attempts, shows up there and in a notification, not as a failure here.

    Available from PostIMAP service_version 1.3.0 onward -- gate the call
    site on postimap.contract.supports_folder_crud() first.

    Args:
        session: Active AsyncSession (caller commits)
        folder_id: Folder to watch or stop watching
        requested: Whether the folder should be watched
    """
    await session.execute(
        update(Folder).where(Folder.id == folder_id).values(idle_requested=requested)
    )


async def acknowledge_notification(session: AsyncSession, notification_id: int) -> None:
    """
    Mark one sync_notifications row as seen.

    acknowledged_at is the only consumer-writable column on this table --
    everything else is PostIMAP's own account of an operation it gave up
    on. Available from PostIMAP service_version 1.3.0 onward -- gate the
    call site on postimap.contract.supports_sync_notifications() first.

    Args:
        session: Active AsyncSession (caller commits)
        notification_id: The sync_notifications row to acknowledge
    """
    await session.execute(
        update(SyncNotification)
        .where(SyncNotification.id == notification_id)
        .values(acknowledged_at=text("now()"))
    )


async def acknowledge_all_notifications(session: AsyncSession, account_id: uuid.UUID) -> None:
    """
    Mark every unacknowledged notification for an account as seen.

    Args:
        session: Active AsyncSession (caller commits)
        account_id: Account whose notifications to acknowledge
    """
    await session.execute(
        update(SyncNotification)
        .where(
            SyncNotification.account_id == account_id,
            SyncNotification.acknowledged_at.is_(None),
        )
        .values(acknowledged_at=text("now()"))
    )


async def insert_outbox(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    kind: str,
    to_addrs: list[str] | None = None,
    cc_addrs: list[str] | None = None,
    bcc_addrs: list[str] | None = None,
    subject: str | None = None,
    body_text: str | None = None,
    body_html: str | None = None,
    from_addr: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    replaces_message_id: uuid.UUID | None = None,
    attachments: Sequence[
        tuple[str, str | None, bytes] | tuple[str, str | None, bytes, str | None]
    ]
    | None = None,
) -> Outbox:
    """
    Insert an outbox row -- a send (kind="send") or a draft (kind="draft").

    PostIMAP composes the MIME message once, sends it for kind="send", and
    appends a copy to the account's sent or drafts special-use folder. The
    appended copy flows back into messages through normal inbound sync,
    thread_id included -- in_reply_to/references is what makes it resolve
    onto the same thread as the message it replies to.

    Args:
        session: Active AsyncSession (caller commits)
        account_id: Account to send/draft from
        kind: "send" or "draft"
        to_addrs: Recipient addresses
        cc_addrs: CC addresses
        bcc_addrs: BCC addresses
        subject: Message subject
        body_text: Plain text body
        body_html: HTML body, optional
        from_addr: Sender address override; falls back to accounts.imap_user
        in_reply_to: The replied-to message's Message-ID header value
        references: Full References chain for threading
        replaces_message_id: The messages.id of the draft this row
            replaces. PostIMAP appends the replacement first and only then
            removes the named message, so editing a draft -- or sending
            one -- costs one insert instead of an expunge-then-insert with
            no ordering between them. Available from PostIMAP
            service_version 1.4.0 onward; gate the call site on
            postimap.contract.supports_draft_edit() first.
        attachments: (filename, content_type, data) tuples, inserted into
            outbox_attachments before the row is picked up. A fourth
            element, content_id, is optional -- set, it embeds the
            attachment inline for a matching cid:<content_id> reference in
            body_html instead of offering it as a download.

    Returns:
        The inserted Outbox row (flushed, not yet committed)
    """
    outbox = Outbox(
        account_id=account_id,
        kind=kind,
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=bcc_addrs,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        in_reply_to=in_reply_to,
        msg_references=references,
        replaces_message_id=replaces_message_id,
    )
    session.add(outbox)
    await session.flush()

    for attachment in attachments or []:
        filename, content_type, data, *rest = attachment
        session.add(
            OutboxAttachment(
                outbox_id=outbox.id,
                filename=filename,
                content_type=content_type,
                data=data,
                content_id=rest[0] if rest else None,
            )
        )

    await session.flush()
    await session.refresh(outbox)
    return outbox


# --- DAV writes (calendars and contacts) --------------------------------
#
# Available from PostIMAP service_version 1.6.0 onward -- gate the call
# site on postimap.contract.supports_dav() first, the same discipline as
# folder CRUD and sync_notifications.


async def create_dav_account(
    session: AsyncSession,
    *,
    name: str,
    url: str,
    username: str,
    password: str,
    is_active: bool = True,
) -> DavAccount:
    """
    Insert a new dav_accounts row -- adding a CalDAV/CardDAV server.

    PostIMAP detects the insert via postimap_events and starts discovery
    and backfill without a restart, the same as create_account().

    Args:
        session: Active AsyncSession (caller commits)
        name: Unique display name
        url: The discovery URL, e.g. https://cloud.example.org/remote.php/dav/
        username: Login username
        password: Password (or app password), plaintext (encoded here per contract)
        is_active: Whether PostIMAP should sync this account

    Returns:
        The inserted DavAccount row (flushed, not yet committed)
    """
    account = DavAccount(
        name=name, url=url, username=username,
        password=format_credential(password), is_active=is_active,
    )
    session.add(account)
    await session.flush()
    await session.refresh(account)
    return account


async def update_dav_account(
    session: AsyncSession,
    dav_account_id: uuid.UUID,
    **fields: Any,
) -> None:
    """
    Update dav_accounts fields, encoding password if present.

    Args:
        session: Active AsyncSession (caller commits)
        dav_account_id: DAV account to update
        **fields: Any of name, password, is_active -- password given as a
            plaintext string and encoded here
    """
    values = dict(fields)
    if values.get("password"):
        values["password"] = format_credential(values["password"])
    if not values:
        return
    await session.execute(
        update(DavAccount).where(DavAccount.id == dav_account_id).values(**values)
    )


async def delete_dav_account(session: AsyncSession, dav_account_id: uuid.UUID) -> None:
    """
    Permanently delete a DAV account and everything mirrored under it.

    Every dav_collections/dav_objects/dav_sync_queue row cascades via ON
    DELETE CASCADE -- there is nothing else to delete. Nothing on the
    server itself is touched; re-adding the account re-syncs it from
    scratch.

    Args:
        session: Active AsyncSession (caller commits)
        dav_account_id: DAV account to delete
    """
    await session.execute(delete(DavAccount).where(DavAccount.id == dav_account_id))


async def create_collection(
    session: AsyncSession,
    *,
    dav_account_id: uuid.UUID,
    kind: str,
    slug: str,
    display_name: str | None = None,
    color: str | None = None,
    description: str | None = None,
) -> DavCollection:
    """
    Insert a dav_collections row -- creating a calendar or address book.

    PostIMAP issues MKCALENDAR (or extended MKCOL for an address book) at
    <home>/<slug>/ and writes href back; a collection that already exists
    at that URL is adopted rather than failing, the same as folder create
    adopting an existing IMAP mailbox.

    Args:
        session: Active AsyncSession (caller commits)
        dav_account_id: The DAV account this collection belongs to
        kind: "calendar" or "addressbook"
        slug: The last path segment the collection gets on the server;
            set once, at creation
        display_name: The server's displayname property
        color: The server's calendar-color property (calendars only)
        description: The server's calendar-description/addressbook-description

    Returns:
        The inserted DavCollection row (flushed, not yet committed)
    """
    collection = DavCollection(
        account_id=dav_account_id, kind=kind, slug=slug,
        display_name=display_name, color=color, description=description,
    )
    session.add(collection)
    await session.flush()
    await session.refresh(collection)
    return collection


async def update_collection(
    session: AsyncSession,
    collection_id: uuid.UUID,
    **fields: Any,
) -> None:
    """
    Update a collection's own server properties.

    Args:
        session: Active AsyncSession (caller commits)
        collection_id: Collection to update
        **fields: Any of display_name, color, description, deleted_at
    """
    if not fields:
        return
    await session.execute(
        update(DavCollection).where(DavCollection.id == collection_id).values(**fields)
    )


async def delete_collection(session: AsyncSession, collection_id: uuid.UUID) -> None:
    """
    Delete a calendar or address book -- destroys every object in it on
    the server, irreversibly. PostIMAP tombstones the mirrored rows in
    the same transaction that records the server's confirmation.

    Args:
        session: Active AsyncSession (caller commits)
        collection_id: Collection to delete
    """
    await session.execute(
        update(DavCollection).where(DavCollection.id == collection_id)
        .values(deleted_at=text("now()"))
    )


async def create_object(
    session: AsyncSession,
    *,
    dav_account_id: uuid.UUID,
    collection_id: uuid.UUID,
    data: str,
) -> DavObject:
    """
    Insert a dav_objects row -- a new event, task, journal entry or contact.

    kind is never named here: a BEFORE INSERT trigger derives it from the
    collection this row is inserted into, and the insert grant deliberately
    does not include it -- naming it explicitly would be one more place
    for it to disagree with the collection. href becomes <uid>.ics/.vcf
    from the parsed UID once the outbound processor's PUT lands; until
    then the parsed columns are NULL, same as a message row before its
    embedding is filled.

    Args:
        session: Active AsyncSession (caller commits)
        dav_account_id: The DAV account this object belongs to
        collection_id: The calendar or address book to create it in
        data: The whole iCalendar or vCard resource, verbatim

    Returns:
        The inserted DavObject row (flushed, not yet committed)
    """
    obj = DavObject(account_id=dav_account_id, collection_id=collection_id, data=data)
    session.add(obj)
    await session.flush()
    await session.refresh(obj)
    return obj


async def replace_object_data(session: AsyncSession, object_id: uuid.UUID, data: str) -> int:
    """
    Replace the whole body of an existing object -- an edit.

    Conditional on the etag the row holds, enforced server-side; a
    concurrent server-side change answers 412 and PostIMAP re-reads the
    server's copy over the row rather than accepting this write.

    Args:
        session: Active AsyncSession (caller commits)
        object_id: Object to edit
        data: The whole replacement iCalendar or vCard resource, verbatim

    Returns:
        The number of rows actually updated (0 or 1)
    """
    result = await session.execute(
        update(DavObject).where(DavObject.id == object_id).values(data=data)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def move_object(
    session: AsyncSession, object_id: uuid.UUID, target_collection_id: uuid.UUID,
) -> int:
    """
    Move an object to a different calendar or address book.

    One statement, the calendar counterpart of move_message(): a BEFORE
    UPDATE trigger nulls etag itself, so this never sets it explicitly --
    etag IS NULL is the pending-move signal once the row lands.

    Args:
        session: Active AsyncSession (caller commits)
        object_id: Object to move
        target_collection_id: Destination collection

    Returns:
        The number of rows actually updated (0 or 1)
    """
    result = await session.execute(
        update(DavObject).where(DavObject.id == object_id)
        .values(collection_id=target_collection_id)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def delete_object(session: AsyncSession, object_id: uuid.UUID) -> int:
    """
    Delete an event, task, journal entry or contact. Soft delete; the row
    survives until retention removes it. Nextcloud moves the resource to
    its own trash bin rather than destroying it, and reports the href as
    gone all the same.

    Args:
        session: Active AsyncSession (caller commits)
        object_id: Object to delete

    Returns:
        The number of rows actually updated (0 or 1)
    """
    result = await session.execute(
        update(DavObject).where(DavObject.id == object_id).values(deleted_at=text("now()"))
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def acknowledge_dav_notification(session: AsyncSession, notification_id: int) -> None:
    """
    Mark one dav_notifications row as seen. acknowledged_at is the only
    consumer-writable column on this table.

    Args:
        session: Active AsyncSession (caller commits)
        notification_id: The dav_notifications row to acknowledge
    """
    await session.execute(
        update(DavNotification).where(DavNotification.id == notification_id)
        .values(acknowledged_at=text("now()"))
    )


async def acknowledge_all_dav_notifications(
    session: AsyncSession, dav_account_id: uuid.UUID,
) -> None:
    """
    Mark every unacknowledged dav_notifications row for a DAV account as seen.

    Args:
        session: Active AsyncSession (caller commits)
        dav_account_id: DAV account whose notifications to acknowledge
    """
    await session.execute(
        update(DavNotification).where(
            DavNotification.account_id == dav_account_id,
            DavNotification.acknowledged_at.is_(None),
        ).values(acknowledged_at=text("now()"))
    )


# --- Guarded writes, for the pipeline runner ---------------------------
#
# Every function below returns the rowcount its UPDATE actually touched
# rather than assuming the caller's intent landed. A message expunged or
# moved between a pipeline run reading the world and applying its effects
# makes the guard predicate fail closed: zero rows, and the caller records
# "not applied" instead of reporting an effect that silently did nothing --
# see rules/executor.py's move_to handler for the bug this exists to
# never reproduce.


async def move_message_guarded(
    session: AsyncSession,
    message_id: uuid.UUID,
    target_folder_id: uuid.UUID,
    *,
    expected_folder_id: uuid.UUID,
) -> int:
    """
    Move a message, only if it is still where the run last saw it.

    Args:
        session: Active AsyncSession (caller commits)
        message_id: Message to move
        target_folder_id: Destination folder
        expected_folder_id: The folder the run believes the message is
            currently in -- a mismatch means someone else moved it first

    Returns:
        1 if the move applied, 0 if the guard failed (message gone,
        expunged, or already moved elsewhere)
    """
    result = await session.execute(
        update(Message)
        .where(
            Message.id == message_id,
            Message.expunged_at.is_(None),
            Message.folder_id == expected_folder_id,
        )
        .values(folder_id=target_folder_id, imap_uid=None)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def set_flags_guarded(session: AsyncSession, message_id: uuid.UUID, **flags: bool) -> int:
    """
    Set flags, only if the message has not been expunged.

    Args:
        session: Active AsyncSession (caller commits)
        message_id: Message to update
        **flags: Any of is_seen, is_flagged, is_answered, is_deleted

    Returns:
        1 if applied, 0 if the message is gone
    """
    if not flags:
        return 0
    result = await session.execute(
        update(Message)
        .where(Message.id == message_id, Message.expunged_at.is_(None))
        .values(**flags)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def set_keywords_delta_guarded(
    session: AsyncSession, message_id: uuid.UUID, *, add: list[str], remove: list[str],
) -> int:
    """
    Add and/or remove keywords as a delta against whatever the array
    currently holds, rather than a read-modify-write of the whole list --
    a delta cannot lose a keyword PostIMAP wrote concurrently the way
    replacing the full array can.

    Args:
        session: Active AsyncSession (caller commits)
        message_id: Message to update
        add: Keywords to add (deduplicated against the result)
        remove: Keywords to remove

    Returns:
        1 if applied, 0 if the message is gone
    """
    if not add and not remove:
        return 0
    result = await session.execute(
        text(
            """
            UPDATE messages
            SET keywords = COALESCE(
                (
                    SELECT array_agg(DISTINCT kw) FROM unnest(keywords || :add) AS kw
                    WHERE NOT (kw = ANY(:remove))
                ),
                '{}'
            )
            WHERE id = :id AND expunged_at IS NULL
            """
        ),
        {"id": message_id, "add": add, "remove": remove},
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def expunge_guarded(session: AsyncSession, message_id: uuid.UUID) -> int:
    """
    Expunge a message, only if it has not already been expunged.

    Args:
        session: Active AsyncSession (caller commits)
        message_id: Message to expunge

    Returns:
        1 if applied, 0 if it was already gone
    """
    result = await session.execute(
        update(Message)
        .where(Message.id == message_id, Message.expunged_at.is_(None))
        .values(expunged_at=text("now()"))
    )
    return result.rowcount or 0  # type: ignore[attr-defined]
