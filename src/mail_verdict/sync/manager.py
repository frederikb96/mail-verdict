"""
Sync manager orchestrating per-account sync cycles.

Coordinates folder discovery, change detection, message fetching,
parsing, and database persistence. Manages periodic sync and
emits events for downstream processing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from mail_verdict.sync.change_detector import ChangeDetector
from mail_verdict.sync.connector import IMAPConnector
from mail_verdict.sync.events import (
    MailReceived,
    MailSpamDetected,
    MailTrashed,
    SyncEvent,
)
from mail_verdict.sync.folders import discover_folders
from mail_verdict.sync.parser import parse_message

if TYPE_CHECKING:
    from mail_verdict.config import AccountConfig, MailVerdictConfig
    from mail_verdict.database.models import Folder
    from mail_verdict.database.repository import (
        AttachmentRepository,
        FolderRepository,
        MailRepository,
    )
    from mail_verdict.rules.bus import EventBus
    from mail_verdict.sync.extensions import AsyncIMAPExtended

logger = logging.getLogger(__name__)


class SyncManager:
    """
    Orchestrates sync cycles for a single IMAP account.

    Runs periodic sync, detects changes, fetches new mails,
    persists to database, and produces events.
    """

    def __init__(
        self,
        account: AccountConfig,
        account_id: uuid.UUID,
        connector: IMAPConnector,
        folder_repo: FolderRepository,
        mail_repo: MailRepository,
        attachment_repo: AttachmentRepository,
        config: MailVerdictConfig,
        event_bus: EventBus | None = None,
    ) -> None:
        """
        Initialize sync manager for an account.

        Args:
            account: Account configuration
            account_id: Account UUID in database
            connector: IMAP connector for this account
            folder_repo: Folder repository
            mail_repo: Mail repository
            attachment_repo: Attachment repository
            config: Global configuration
            event_bus: Optional event bus for broadcasting events to rules/SSE
        """
        self._account = account
        self._account_id = account_id
        self._connector = connector
        self._folder_repo = folder_repo
        self._mail_repo = mail_repo
        self._attachment_repo = attachment_repo
        self._config = config
        self._change_detector = ChangeDetector(folder_repo, mail_repo)
        self._event_queue: asyncio.Queue[SyncEvent] = asyncio.Queue(maxsize=1000)
        self._event_bus = event_bus
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def account_name(self) -> str:
        """Account identifier."""
        return self._account.name

    @property
    def event_queue(self) -> asyncio.Queue[SyncEvent]:
        """Queue for sync events consumed by downstream processors."""
        return self._event_queue

    async def start(self) -> None:
        """Start the periodic sync loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(
            self._sync_loop(),
            name=f"sync-{self._account.name}",
        )
        logger.info(
            "Sync manager started",
            extra={"account": self._account.name},
        )

    async def stop(self) -> None:
        """Stop the sync loop and wait for completion."""
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info(
            "Sync manager stopped",
            extra={"account": self._account.name},
        )

    async def sync_once(self) -> list[SyncEvent]:
        """
        Run a single sync cycle.

        Discovers folders, detects changes in each, fetches new messages.

        Returns:
            All events generated during this sync cycle
        """
        all_events: list[SyncEvent] = []

        try:
            async with self._connector.acquire() as conn:
                # Discover/update folders
                await discover_folders(
                    conn,
                    self._account_id,
                    self._folder_repo,
                    auto_detect=self._config.sync.auto_detect_folders,
                )

                # Sync each folder
                folders = await self._folder_repo.get_by_account(self._account_id)
                for folder in folders:
                    try:
                        events = await self._sync_folder(conn, folder)
                        all_events.extend(events)
                    except Exception as exc:
                        logger.error(
                            "Folder sync failed",
                            extra={
                                "account": self._account.name,
                                "folder": folder.imap_name,
                                "error": str(exc),
                            },
                            exc_info=True,
                        )

        except Exception as exc:
            logger.error(
                "Sync cycle failed",
                extra={
                    "account": self._account.name,
                    "error": str(exc),
                },
            )

        # Enqueue events for spam processor
        for event in all_events:
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "Event queue full, dropping event",
                    extra={"account": self._account.name},
                )
                break

        # Emit events to the event bus for rules engine and SSE
        if self._event_bus:
            for event in all_events:
                try:
                    await self._event_bus.emit(event)
                except Exception as exc:
                    logger.warning(
                        "Failed to emit event to bus",
                        extra={
                            "account": self._account.name,
                            "event": type(event).__name__,
                            "error": str(exc),
                        },
                    )

        return all_events

    async def _sync_loop(self) -> None:
        """Periodic sync loop running until stopped."""
        while self._running:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Sync loop error",
                    extra={
                        "account": self._account.name,
                        "error": str(exc),
                    },
                )

            if self._running:
                try:
                    await asyncio.sleep(self._config.sync.poll_interval_seconds)
                except asyncio.CancelledError:
                    break

    async def _sync_folder(
        self,
        conn: AsyncIMAPExtended,
        folder: Folder,
    ) -> list[SyncEvent]:
        """
        Sync a single folder: detect changes, fetch new messages, update state.

        Args:
            conn: Active IMAP connection
            folder: Folder to sync
        """
        changeset, events = await self._change_detector.detect_changes(
            conn,
            self._account_id,
            folder,
        )

        # Fetch and persist new messages
        if changeset.new_uids:
            await self._fetch_and_store_messages(
                conn,
                folder,
                changeset.new_uids,
            )

        # Update flag changes
        for uf in changeset.flag_changes:
            try:
                await self._mail_repo.upsert_mail(
                    account_id=self._account_id,
                    folder_id=folder.id,
                    uid=uf.uid,
                    is_read="\\Seen" in uf.flags,
                    is_flagged="\\Flagged" in uf.flags,
                    modseq=uf.modseq,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to update flags",
                    extra={"uid": uf.uid, "error": str(exc)},
                )

        # Mark deleted mails
        for uid in changeset.deleted_uids:
            try:
                await self._mail_repo.upsert_mail(
                    account_id=self._account_id,
                    folder_id=folder.id,
                    uid=uid,
                    is_deleted=True,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to mark deleted",
                    extra={"uid": uid, "error": str(exc)},
                )

        # Update sync state only after all changes are persisted
        await self._folder_repo.update_state(
            folder_id=folder.id,
            uidvalidity=changeset.new_uidvalidity,
            uidnext=changeset.new_uidnext,
            highestmodseq=changeset.new_highestmodseq,
            last_synced_at=datetime.now(timezone.utc),
        )

        # Reclassify events based on folder type
        events = self._classify_folder_events(events, folder)

        if changeset.new_uids or changeset.deleted_uids:
            logger.info(
                "Folder synced",
                extra={
                    "account": self._account.name,
                    "folder": folder.imap_name,
                    "new": len(changeset.new_uids),
                    "deleted": len(changeset.deleted_uids),
                    "flag_changes": len(changeset.flag_changes),
                },
            )

        return events

    async def _fetch_and_store_messages(
        self,
        conn: AsyncIMAPExtended,
        folder: Folder,
        uids: list[int],
    ) -> None:
        """
        Fetch full messages for given UIDs and store in database.

        Args:
            conn: Active IMAP connection (folder already selected)
            folder: Current folder
            uids: UIDs to fetch
        """
        if not uids:
            return

        uid_set = ",".join(str(u) for u in uids)

        response = await conn.client.uid("FETCH", uid_set, "(RFC822 FLAGS)")

        if response.result != "OK":
            logger.error(
                "FETCH failed",
                extra={"folder": folder.imap_name, "uid_set": uid_set},
            )
            return

        # Parse FETCH responses.
        # aioimaplib returns:
        #   bytearray: message body (literal data)
        #   bytes: command response lines (metadata, flags, status)
        current_uid: int | None = None
        current_flags: set[str] = set()
        current_body: bytes | None = None

        uid_re = re.compile(r"UID\s+(\d+)")
        flags_re = re.compile(r"FLAGS\s+\(([^)]*)\)")

        for line in response.lines:
            # bytearray = literal message body from IMAP
            if isinstance(line, bytearray):
                if current_uid is not None:
                    current_body = bytes(line)
                continue

            # bytes or str = command response (metadata, trailing flags, status)
            text = line.decode(errors="replace") if isinstance(line, bytes) else str(line)

            uid_match = uid_re.search(text)
            if uid_match:
                # New message — store previous if exists
                if current_uid is not None and current_body is not None:
                    await self._store_parsed_message(
                        folder, current_uid, current_body, current_flags
                    )

                current_uid = int(uid_match.group(1))
                current_flags = set()
                current_body = None

                flags_match = flags_re.search(text)
                if flags_match:
                    raw_flags = flags_match.group(1)
                    current_flags = {f.strip() for f in raw_flags.split() if f.strip()}
            else:
                # Trailing FLAGS line or status line
                flags_match = flags_re.search(text)
                if flags_match and current_uid is not None:
                    raw_flags = flags_match.group(1)
                    current_flags = {f.strip() for f in raw_flags.split() if f.strip()}

        # Store the last message
        if current_uid is not None and current_body is not None:
            await self._store_parsed_message(folder, current_uid, current_body, current_flags)

    async def _store_parsed_message(
        self,
        folder: Folder,
        uid: int,
        raw_bytes: bytes,
        flags: set[str],
    ) -> None:
        """
        Parse and store a single message.

        Args:
            folder: Folder containing the message
            uid: IMAP UID
            raw_bytes: Raw RFC 2822 message
            flags: Current IMAP flags
        """
        try:
            parsed = parse_message(raw_bytes)

            mail = await self._mail_repo.upsert_mail(
                account_id=self._account_id,
                folder_id=folder.id,
                uid=uid,
                message_id=parsed.message_id,
                subject=parsed.subject,
                from_addr=parsed.from_addr,
                to_addrs={"addrs": parsed.to_addrs} if parsed.to_addrs else None,
                cc_addrs={"addrs": parsed.cc_addrs} if parsed.cc_addrs else None,
                bcc_addrs={"addrs": parsed.bcc_addrs} if parsed.bcc_addrs else None,
                body_text=parsed.body_text,
                body_html=parsed.body_html,
                raw_headers=parsed.raw_headers,
                raw_source=raw_bytes,
                received_at=parsed.date,
                size_bytes=parsed.size_bytes,
                is_read="\\Seen" in flags,
                is_flagged="\\Flagged" in flags,
                is_deleted="\\Deleted" in flags,
                dkim_pass=parsed.auth.dkim_pass,
                spf_pass=parsed.auth.spf_pass,
                dmarc_pass=parsed.auth.dmarc_pass,
            )

            # Store attachments
            for att in parsed.attachments:
                await self._attachment_repo.create(
                    mail_id=mail.id,
                    filename=att.filename,
                    content_type=att.content_type,
                    content_id=att.content_id,
                    size_bytes=att.size_bytes,
                    data=att.data,
                )

        except Exception as exc:
            logger.error(
                "Failed to parse/store message",
                extra={
                    "folder": folder.imap_name,
                    "uid": uid,
                    "error": str(exc),
                },
            )

    def _classify_folder_events(
        self,
        events: list[SyncEvent],
        folder: Folder,
    ) -> list[SyncEvent]:
        """
        Reclassify events based on folder special-use type.

        Mail arriving in trash -> MailTrashed, in junk -> MailSpamDetected.

        Args:
            events: Raw events from change detection
            folder: Folder with special_use type
        """
        if not folder.special_use:
            return events

        from mail_verdict.database.models import SpecialUse

        classified: list[SyncEvent] = []

        for event in events:
            if isinstance(event, MailReceived):
                if folder.special_use == SpecialUse.TRASH:
                    classified.append(
                        MailTrashed(
                            account_id=event.account_id,
                            folder_id=event.folder_id,
                            uid=event.uid,
                            message_id=event.message_id,
                        )
                    )
                elif folder.special_use == SpecialUse.JUNK:
                    classified.append(
                        MailSpamDetected(
                            account_id=event.account_id,
                            folder_id=event.folder_id,
                            uid=event.uid,
                            message_id=event.message_id,
                        )
                    )
                else:
                    classified.append(event)
            else:
                classified.append(event)

        return classified
