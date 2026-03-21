"""
Sync manager orchestrating per-account sync cycles.

Coordinates folder discovery, change detection, two-phase message fetching
(headers first, bodies in background), parsing, and database persistence.
Manages periodic sync and emits events for downstream processing.

Uses imap-tools MailBox with asyncio.to_thread for all IMAP operations.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from imap_tools import AND, BaseMailBox

from mail_verdict.core.sanitizer import sanitize_email_html
from mail_verdict.sync.change_detector import ChangeDetector
from mail_verdict.sync.connector import IMAPConnector, is_connection_error
from mail_verdict.sync.events import (
    MailReceived,
    MailSpamDetected,
    MailTrashed,
    SyncEvent,
)
from mail_verdict.sync.folders import discover_folders
from mail_verdict.sync.parser import parse_message
from mail_verdict.sync.tracker import SyncPhase, SyncTracker

if TYPE_CHECKING:
    from typing import Any

    from mail_verdict.database.models import Folder
    from mail_verdict.database.repository import (
        AccountRepository,
        AttachmentRepository,
        FolderRepository,
        MailRepository,
    )
    from mail_verdict.rules.bus import EventBus
    from mail_verdict.sync.connector import AccountConnConfig

logger = logging.getLogger(__name__)


class SyncManager:
    """
    Orchestrates sync cycles for a single IMAP account.

    Runs periodic sync, detects changes, fetches new mails in two phases
    (headers first for immediate display, bodies in background),
    persists to database, and produces events.
    """

    def __init__(
        self,
        account: AccountConnConfig,
        account_id: uuid.UUID,
        connector: IMAPConnector,
        folder_repo: FolderRepository,
        mail_repo: MailRepository,
        attachment_repo: AttachmentRepository,
        sync_settings: dict[str, Any],
        event_bus: EventBus | None = None,
        tracker: SyncTracker | None = None,
        account_repo: AccountRepository | None = None,
    ) -> None:
        """
        Initialize sync manager for an account.

        Args:
            account: Account connection config
            account_id: Account UUID in database
            connector: IMAP connector for this account
            folder_repo: Folder repository
            mail_repo: Mail repository
            attachment_repo: Attachment repository
            sync_settings: Sync settings dict
            event_bus: Optional event bus for broadcasting events to rules/SSE
            tracker: Optional sync progress tracker for SSE updates
            account_repo: Optional account repository for state updates
        """
        self._account = account
        self._account_id = account_id
        self._connector = connector
        self._folder_repo = folder_repo
        self._mail_repo = mail_repo
        self._attachment_repo = attachment_repo
        self._sync_settings = sync_settings
        self._account_repo = account_repo
        self._change_detector = ChangeDetector(folder_repo, mail_repo)
        self._event_queue: asyncio.Queue[SyncEvent] = asyncio.Queue(maxsize=1000)
        self._event_bus = event_bus
        self._tracker = tracker
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._trigger_event = asyncio.Event()
        self._sync_lock = asyncio.Lock()
        self._needs_reconciliation = False

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
        self._task.add_done_callback(self._task_done_callback)
        logger.info(
            "Sync manager started",
            extra={"account": self._account.name},
        )

    @staticmethod
    def _task_done_callback(task: asyncio.Task[None]) -> None:
        """Log if the sync task exits unexpectedly."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("Sync task crashed: %s", exc, exc_info=exc)

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

    async def trigger_now(self) -> None:
        """Wake the sync loop to run immediately."""
        self._trigger_event.set()

    async def fetch_body_for_mail(
        self,
        folder_imap_name: str,
        uid: int,
        folder_id: uuid.UUID,
    ) -> bool:
        """
        On-demand fetch of a single mail's body from IMAP.

        Used when GET /mails/:id is called and body_synced=False.
        Fetches full RFC822 content, stores body/attachments, sets body_synced=True.

        Args:
            folder_imap_name: IMAP folder name to select
            uid: IMAP UID of the message
            folder_id: Database folder UUID

        Returns:
            True if body was fetched and stored successfully
        """
        try:
            async with self._connector.acquire() as conn:
                # Select the folder
                def _select_folder() -> None:
                    conn.folder.set(folder_imap_name)

                await asyncio.to_thread(_select_folder)

                uid_str = str(uid)

                def _sync_fetch_body() -> dict[str, object] | None:
                    """Fetch single message body in thread."""
                    for msg in conn.fetch(
                        AND(uid=uid_str),
                        mark_seen=False,
                    ):
                        raw_bytes = msg.obj.as_bytes() if msg.obj else b""
                        attachments = []
                        for att in msg.attachments:
                            attachments.append({
                                "filename": att.filename,
                                "content_type": att.content_type,
                                "content_id": att.content_id,
                                "size_bytes": att.size,
                                "data": att.payload,
                            })
                        return {
                            "uid": int(msg.uid) if msg.uid else 0,
                            "body_text": msg.text,
                            "body_html": msg.html,
                            "raw_source": raw_bytes,
                            "raw_headers": str(msg.headers) if msg.headers else None,
                            "message_id": msg.headers.get("message-id", [""])[0]
                            if msg.headers else None,
                            "attachments": attachments,
                        }
                    return None

                body = await asyncio.to_thread(_sync_fetch_body)

                if body is None:
                    logger.warning(
                        "On-demand body fetch returned no message",
                        extra={"folder": folder_imap_name, "uid": uid},
                    )
                    return False

                raw_source = body["raw_source"]
                assert isinstance(raw_source, bytes)
                parsed = parse_message(raw_source)

                raw_html = str(body["body_html"]) if body["body_html"] else None
                sanitized_html = sanitize_email_html(raw_html) if raw_html else None

                await self._mail_repo.upsert_mail(
                    account_id=self._account_id,
                    folder_id=folder_id,
                    uid=uid,
                    message_id=str(body["message_id"]) if body["message_id"] else None,
                    body_text=str(body["body_text"]) if body["body_text"] else None,
                    body_html=sanitized_html,
                    raw_headers=parsed.raw_headers,
                    raw_source=raw_source,
                    dkim_pass=parsed.auth.dkim_pass,
                    spf_pass=parsed.auth.spf_pass,
                    dmarc_pass=parsed.auth.dmarc_pass,
                    body_synced=True,
                )

                # Store attachments
                attachments = body["attachments"]
                assert isinstance(attachments, list)
                for att in attachments:
                    assert isinstance(att, dict)
                    mail = await self._mail_repo.get_by_folder_and_uid(folder_id, uid)
                    if mail:
                        ct = att["content_type"]
                        ci = att["content_id"]
                        sz = att["size_bytes"]
                        await self._attachment_repo.create(
                            mail_id=mail.id,
                            filename=str(att["filename"]) if att["filename"] else None,
                            content_type=str(ct) if ct else None,
                            content_id=str(ci) if ci else None,
                            size_bytes=int(str(sz)) if sz else None,
                            data=att["data"] if isinstance(att["data"], bytes) else None,
                        )

                logger.info(
                    "On-demand body fetch complete",
                    extra={"folder": folder_imap_name, "uid": uid},
                )
                return True

        except Exception as exc:
            logger.error(
                "On-demand body fetch failed",
                extra={"folder": folder_imap_name, "uid": uid, "error": str(exc)},
            )
            return False

    async def sync_once(self) -> list[SyncEvent]:
        """
        Run a single sync cycle (serialized via lock).

        Only one sync runs at a time. IDLE, poll, and manual triggers
        are serialized -- overlapping calls wait for the current sync.

        Returns:
            All events generated during this sync cycle
        """
        async with self._sync_lock:
            return await self._sync_once_inner()

    async def _sync_once_inner(self) -> list[SyncEvent]:
        """Actual sync implementation (must be called under _sync_lock)."""
        all_events: list[SyncEvent] = []
        total_new = 0
        total_errors = 0
        reconciling = self._needs_reconciliation

        if reconciling:
            logger.info(
                "Post-reconnect reconciliation: forcing full UID diff",
                extra={"account": self._account.name},
            )

        if self._tracker:
            await self._tracker.update(phase=SyncPhase.PREFLIGHT)
            await self._update_account_state(SyncPhase.PREFLIGHT)

        try:
            async with self._connector.acquire() as conn:
                # Discover/update folders
                await discover_folders(
                    conn,
                    self._account_id,
                    self._folder_repo,
                    auto_detect=bool(self._sync_settings.get("auto_detect_folders", True)),
                )

                folders = await self._folder_repo.get_by_account(self._account_id)
                folder_count = len(folders)

                # When reconciling after connection loss, clear stored
                # highestmodseq so the change detector uses full UID diff
                # (tier 3) instead of CONDSTORE (tier 2). IMAP server state wins.
                if reconciling:
                    for folder in folders:
                        if folder.highestmodseq:
                            await self._folder_repo.update_state(
                                folder_id=folder.id,
                                highestmodseq=0,
                            )
                    # Re-fetch folders with cleared modseq
                    folders = await self._folder_repo.get_by_account(self._account_id)

                # Preflight: get message counts per folder via STATUS
                total_messages = 0
                for folder in folders:
                    count = await self._get_folder_message_count(
                        conn, folder.imap_name
                    )
                    total_messages += count

                if self._tracker:
                    await self._tracker.update(
                        phase=SyncPhase.SYNCING,
                        folder_total=folder_count,
                        total_messages=total_messages,
                    )
                    await self._update_account_state(SyncPhase.SYNCING)

                # Sync each folder with progress events
                for idx, folder in enumerate(folders):
                    if not self._running:
                        break

                    if self._tracker:
                        await self._tracker.update(
                            folder_name=folder.imap_name,
                            folder_index=idx + 1,
                            folder_synced=0,
                            folder_messages=0,
                        )

                    folder_new = 0
                    try:
                        events = await self._sync_folder(conn, folder)
                        all_events.extend(events)
                        folder_new = sum(
                            1 for e in events if isinstance(e, MailReceived)
                        )
                        total_new += folder_new
                    except Exception as exc:
                        total_errors += 1
                        logger.error(
                            "Folder sync failed",
                            extra={
                                "account": self._account.name,
                                "folder": folder.imap_name,
                                "error": str(exc),
                            },
                            exc_info=True,
                        )
                        if self._tracker:
                            await self._tracker.update(
                                errors=total_errors,
                                last_error=f"Folder {folder.imap_name}: {exc}",
                            )
                        # Re-raise connection errors so _sync_loop can handle backoff/drain
                        if is_connection_error(exc):
                            raise

                    if self._tracker:
                        await self._tracker.update(new_mails=total_new)

        except Exception as exc:
            total_errors += 1
            logger.error(
                "Sync cycle failed",
                extra={
                    "account": self._account.name,
                    "error": str(exc),
                },
            )
            if self._tracker:
                await self._tracker.update(
                    phase=SyncPhase.ERROR,
                    errors=total_errors,
                    last_error=str(exc),
                )
                await self._update_account_state(SyncPhase.ERROR)
            # Re-raise connection errors so _sync_loop can handle backoff/drain
            if is_connection_error(exc):
                raise

        if self._tracker and self._tracker.phase != SyncPhase.ERROR:
            await self._tracker.update(phase=SyncPhase.COMPLETE)
            await self._update_account_state(SyncPhase.COMPLETE)

        # Clear reconciliation flag on successful sync (no outer exception)
        if reconciling and total_errors == 0:
            self._needs_reconciliation = False
            logger.info(
                "Post-reconnect reconciliation complete",
                extra={"account": self._account.name},
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
        """
        Periodic sync loop with connection loss detection and auto-reconnect.

        On connection loss, drains the pool and uses exponential backoff
        (base_delay to max_delay) before retrying. Backoff resets on
        successful sync.
        """
        consecutive_conn_failures = 0

        while self._running:
            try:
                await self.sync_once()
                # Successful sync: reset backoff counter
                consecutive_conn_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_connection_error(exc):
                    consecutive_conn_failures += 1
                    # Drain stale connections so next acquire() creates fresh ones
                    await self._connector.drain_pool()

                    retry_settings = self._sync_settings
                    base_delay = float(retry_settings.get("base_delay_seconds", 1.0))
                    max_delay = float(retry_settings.get("max_delay_seconds", 60.0))
                    exp_base = float(retry_settings.get("exponential_base", 2.0))
                    delay = min(
                        base_delay * (exp_base ** (consecutive_conn_failures - 1)),
                        max_delay,
                    )

                    logger.warning(
                        "Connection lost during sync, reconnecting with backoff",
                        extra={
                            "account": self._account.name,
                            "consecutive_failures": consecutive_conn_failures,
                            "backoff_seconds": delay,
                            "error": str(exc),
                        },
                    )

                    # Next successful sync must reconcile with server state
                    self._needs_reconciliation = True

                    if self._tracker:
                        await self._tracker.update(
                            phase=SyncPhase.ERROR,
                            last_error=f"Connection lost: {exc}",
                        )
                        await self._update_account_state(SyncPhase.ERROR)

                    if self._running:
                        await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        "Sync loop error",
                        extra={
                            "account": self._account.name,
                            "error": str(exc),
                        },
                    )

            if self._running:
                try:
                    interval = int(self._sync_settings.get("poll_interval_seconds", 300))
                    jitter = random.uniform(0, interval * 0.1)
                    self._trigger_event.clear()
                    try:
                        await asyncio.wait_for(
                            self._trigger_event.wait(), timeout=interval + jitter,
                        )
                    except asyncio.TimeoutError:
                        pass
                except asyncio.CancelledError:
                    break

    async def _get_folder_message_count(
        self,
        mailbox: BaseMailBox,
        folder_name: str,
    ) -> int:
        """
        Get message count for a folder via STATUS (without changing selected folder).

        Args:
            mailbox: Authenticated BaseMailBox
            folder_name: IMAP folder name
        """
        def _sync() -> int:
            try:
                status = mailbox.folder.status(folder_name, options=("MESSAGES",))
                return status.get("MESSAGES", 0)
            except Exception:
                return 0

        return await asyncio.to_thread(_sync)

    async def _sync_folder(
        self,
        conn: BaseMailBox,
        folder: Folder,
    ) -> list[SyncEvent]:
        """
        Sync a single folder: detect changes, fetch new messages, update state.

        Phase 1: Fetch headers only (fast, user sees mails immediately)
        Phase 2: Fetch full bodies in background

        Args:
            conn: Authenticated imap-tools BaseMailBox
            folder: Folder to sync
        """
        changeset, events = await self._change_detector.detect_changes(
            conn,
            self._account_id,
            folder,
        )

        # UIDVALIDITY changed: purge all local mails for this folder
        if changeset.uidvalidity_changed:
            logger.info(
                "UIDVALIDITY changed, purging local mails for folder",
                extra={
                    "account": self._account.name,
                    "folder": folder.imap_name,
                },
            )
            await self._mail_repo.delete_by_folder(folder.id)

        # Phase 1: Fetch headers for new messages (fast)
        if changeset.new_uids:
            await self._fetch_and_store_headers(conn, folder, changeset.new_uids)

        # Phase 2: Fetch full bodies for new messages (background)
        if changeset.new_uids:
            await self._fetch_and_store_bodies(conn, folder, changeset.new_uids)

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

    async def _fetch_and_store_headers(
        self,
        conn: BaseMailBox,
        folder: Folder,
        uids: list[int],
    ) -> None:
        """
        Phase 1: Fetch headers only for given UIDs and store in database.

        Fast fetch that gets subject, from, to, date, flags, size.
        Stores with headers_synced=True, body_synced=False.

        Args:
            conn: Authenticated imap-tools BaseMailBox (folder already selected)
            folder: Current folder
            uids: UIDs to fetch headers for
        """
        if not uids:
            return

        batch_size = 50
        total = len(uids)

        if self._tracker:
            await self._tracker.update(folder_messages=total, folder_synced=0)

        for batch_start in range(0, total, batch_size):
            if not self._running:
                break

            batch = uids[batch_start: batch_start + batch_size]
            uid_str = ",".join(str(u) for u in batch)

            def _sync_fetch_headers(uid_criteria: str = uid_str) -> list[dict[str, object]]:
                """Fetch headers in thread."""
                results: list[dict[str, object]] = []
                for msg in conn.fetch(
                    AND(uid=uid_criteria),
                    headers_only=True,
                    mark_seen=False,
                ):
                    results.append({
                        "uid": int(msg.uid) if msg.uid else 0,
                        "subject": msg.subject,
                        "from_addr": msg.from_,
                        "to_addrs": [str(addr) for addr in msg.to],
                        "cc_addrs": [str(addr) for addr in msg.cc],
                        "bcc_addrs": [str(addr) for addr in msg.bcc],
                        "date": msg.date,
                        "flags": set(msg.flags),
                        "size_bytes": msg.size,
                    })
                return results

            headers_list = await asyncio.to_thread(_sync_fetch_headers)

            for hdr in headers_list:
                uid = int(str(hdr["uid"]))
                flags = hdr["flags"]
                assert isinstance(flags, set)

                to_addrs = hdr["to_addrs"]
                cc_addrs = hdr["cc_addrs"]
                bcc_addrs = hdr["bcc_addrs"]
                date_val = hdr["date"]

                try:
                    await self._mail_repo.upsert_mail(
                        account_id=self._account_id,
                        folder_id=folder.id,
                        uid=uid,
                        subject=str(hdr["subject"]) if hdr["subject"] else None,
                        from_addr=str(hdr["from_addr"]) if hdr["from_addr"] else None,
                        to_addrs=list(to_addrs) if isinstance(to_addrs, list) else None,
                        cc_addrs=list(cc_addrs) if isinstance(cc_addrs, list) else None,
                        bcc_addrs=list(bcc_addrs) if isinstance(bcc_addrs, list) else None,
                        received_at=date_val if isinstance(date_val, datetime) else None,
                        size_bytes=int(str(hdr["size_bytes"])) if hdr["size_bytes"] else None,
                        is_read="\\Seen" in flags,
                        is_flagged="\\Flagged" in flags,
                        is_deleted="\\Deleted" in flags,
                        headers_synced=True,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to store header",
                        extra={"uid": uid, "error": str(exc)},
                    )

            folder_synced = min(batch_start + len(batch), total)
            if self._tracker:
                await self._tracker.update(
                    folder_synced=folder_synced,
                    synced=self._tracker.synced + len(batch),
                )

    async def _fetch_and_store_bodies(
        self,
        conn: BaseMailBox,
        folder: Folder,
        uids: list[int],
    ) -> None:
        """
        Phase 2: Fetch full message bodies for given UIDs and update database.

        Fetches full RFC822 content including body text, HTML, and attachments.

        Args:
            conn: Authenticated imap-tools BaseMailBox (folder already selected)
            folder: Current folder
            uids: UIDs to fetch bodies for
        """
        if not uids:
            return

        batch_size = 20
        total = len(uids)

        for batch_start in range(0, total, batch_size):
            if not self._running:
                break

            batch = uids[batch_start: batch_start + batch_size]
            uid_str = ",".join(str(u) for u in batch)

            def _sync_fetch_bodies(uid_criteria: str = uid_str) -> list[dict[str, object]]:
                """Fetch full messages in thread."""
                results: list[dict[str, object]] = []
                for msg in conn.fetch(
                    AND(uid=uid_criteria),
                    mark_seen=False,
                ):
                    raw_bytes = msg.obj.as_bytes() if msg.obj else b""
                    attachments = []
                    for att in msg.attachments:
                        attachments.append({
                            "filename": att.filename,
                            "content_type": att.content_type,
                            "content_id": att.content_id,
                            "size_bytes": att.size,
                            "data": att.payload,
                        })
                    results.append({
                        "uid": int(msg.uid) if msg.uid else 0,
                        "body_text": msg.text,
                        "body_html": msg.html,
                        "raw_source": raw_bytes,
                        "raw_headers": str(msg.headers) if msg.headers else None,
                        "message_id": msg.headers.get("message-id", [""])[0]
                        if msg.headers else None,
                        "flags": set(msg.flags),
                        "attachments": attachments,
                    })
                return results

            bodies_list = await asyncio.to_thread(_sync_fetch_bodies)

            for body in bodies_list:
                uid = int(str(body["uid"]))
                raw_source = body["raw_source"]
                assert isinstance(raw_source, bytes)

                try:
                    # Parse for auth headers
                    parsed = parse_message(raw_source)

                    raw_html = str(body["body_html"]) if body["body_html"] else None
                    sanitized_html = sanitize_email_html(raw_html) if raw_html else None

                    await self._mail_repo.upsert_mail(
                        account_id=self._account_id,
                        folder_id=folder.id,
                        uid=uid,
                        message_id=str(body["message_id"]) if body["message_id"] else None,
                        body_text=str(body["body_text"]) if body["body_text"] else None,
                        body_html=sanitized_html,
                        raw_headers=parsed.raw_headers,
                        raw_source=raw_source,
                        dkim_pass=parsed.auth.dkim_pass,
                        spf_pass=parsed.auth.spf_pass,
                        dmarc_pass=parsed.auth.dmarc_pass,
                        body_synced=True,
                    )

                    # Store attachments
                    attachments = body["attachments"]
                    assert isinstance(attachments, list)
                    for att in attachments:
                        assert isinstance(att, dict)
                        mail = await self._mail_repo.get_by_folder_and_uid(
                            folder.id, uid
                        )
                        if mail:
                            ct = att["content_type"]
                            ci = att["content_id"]
                            sz = att["size_bytes"]
                            await self._attachment_repo.create(
                                mail_id=mail.id,
                                filename=str(att["filename"]) if att["filename"] else None,
                                content_type=str(ct) if ct else None,
                                content_id=str(ci) if ci else None,
                                size_bytes=int(str(sz)) if sz else None,
                                data=att["data"] if isinstance(att["data"], bytes) else None,
                            )

                except Exception as exc:
                    logger.error(
                        "Failed to store body",
                        extra={
                            "folder": folder.imap_name,
                            "uid": uid,
                            "error": str(exc),
                        },
                    )

    async def _update_account_state(self, sync_phase: SyncPhase) -> None:
        """
        Update Account.state in DB based on SyncTracker phase.

        Maps sync phases to Account states:
        - PREFLIGHT/SYNCING -> SYNCING
        - COMPLETE -> ACTIVE (when no errors)
        - ERROR -> ERROR

        Args:
            sync_phase: Current SyncPhase
        """
        from mail_verdict.database.models import AccountState

        if not self._account_repo:
            return

        state_map = {
            SyncPhase.PREFLIGHT: AccountState.SYNCING,
            SyncPhase.SYNCING: AccountState.SYNCING,
            SyncPhase.COMPLETE: AccountState.ACTIVE,
            SyncPhase.ERROR: AccountState.ERROR,
        }

        new_state = state_map.get(sync_phase)
        if new_state:
            try:
                await self._account_repo.update_state(self._account_id, new_state)
            except Exception as exc:
                logger.warning(
                    "Failed to update account state",
                    extra={
                        "account": self._account.name,
                        "phase": sync_phase.value,
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
