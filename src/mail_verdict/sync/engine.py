"""
Sync engine orchestrator.

Top-level coordination of IMAP sync for all configured accounts.
Creates connectors, sync managers, IDLE watchers, and action propagators
per account. Integrates with the FastAPI lifespan for startup/shutdown.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING

from mail_verdict.sync.actions import ActionPropagator
from mail_verdict.sync.connector import IMAPConnector
from mail_verdict.sync.idle import IdleWatcher
from mail_verdict.sync.manager import SyncManager
from mail_verdict.sync.smtp_client import SMTPClient
from mail_verdict.sync.tracker import SyncTracker

if TYPE_CHECKING:
    from typing import Any

    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.config import InfraConfig
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.rules.bus import EventBus
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)


class AccountSync:
    """All sync components for a single account."""

    def __init__(
        self,
        account_id: uuid.UUID,
        connector: IMAPConnector,
        manager: SyncManager,
        idle_watcher: IdleWatcher,
        action_propagator: ActionPropagator,
        smtp_client: SMTPClient | None,
        tracker: SyncTracker | None = None,
    ) -> None:
        """
        Initialize account sync bundle.

        Args:
            account_id: Database UUID for this account
            connector: IMAP connector
            manager: Sync manager
            idle_watcher: IDLE watcher
            action_propagator: Action propagator
            smtp_client: Optional SMTP client
            tracker: Optional sync progress tracker
        """
        self.account_id = account_id
        self.connector = connector
        self.manager = manager
        self.idle_watcher = idle_watcher
        self.action_propagator = action_propagator
        self.smtp_client = smtp_client
        self.tracker = tracker


class SyncEngine:
    """
    Top-level sync engine managing all account syncs.

    Creates and manages per-account sync infrastructure:
    connector, sync manager, IDLE watcher, action propagator, SMTP client.
    """

    def __init__(
        self,
        config: InfraConfig,
        db: DatabaseConnection,
        event_bus: EventBus | None = None,
        settings_service: SettingsService | None = None,
        event_ring: EventRing | None = None,
    ) -> None:
        """
        Initialize sync engine.

        Args:
            config: Infrastructure configuration
            db: Database connection for repository access
            event_bus: Optional event bus for broadcasting sync events
            settings_service: Application settings service
            event_ring: Optional EventRing for SSE event emission
        """
        self._config = config
        self._db = db
        self._event_bus = event_bus
        self._settings = settings_service
        self._event_ring = event_ring
        self._accounts: dict[str, AccountSync] = {}

    async def start(self) -> None:
        """Start sync for all active accounts from the database."""
        from sqlalchemy import select

        from mail_verdict.core.encryption import decrypt
        from mail_verdict.database.models import Account
        from mail_verdict.database.repository import (
            AttachmentRepository,
            FolderRepository,
            MailRepository,
        )
        from mail_verdict.sync.connector import AccountConnConfig

        # Load active accounts from DB
        async with self._db.session() as session:
            result = await session.execute(
                select(Account).where(Account.is_active.is_(True))
            )
            db_accounts = list(result.scalars().all())

        if not db_accounts:
            logger.info("No active accounts in database, sync engine idle")
            return

        from mail_verdict.database.repository import AccountRepository

        account_repo = AccountRepository(self._db)
        folder_repo = FolderRepository(self._db)
        mail_repo = MailRepository(self._db)
        attachment_repo = AttachmentRepository(self._db)

        from mail_verdict.core.retry import RetryConfig as RC

        sync_settings = self._settings.get("sync") if self._settings else {}
        retry_settings = self._settings.get("retry") if self._settings else {}
        retry_config = RC.from_settings(retry_settings)

        for acct in db_accounts:
            try:
                imap_password = decrypt(acct.imap_password) if acct.imap_password else ""
                smtp_password = decrypt(acct.smtp_password) if acct.smtp_password else ""

                conn_config = AccountConnConfig(
                    name=acct.name,
                    host=acct.imap_host,
                    port=acct.imap_port,
                    username=acct.imap_user,
                    password=imap_password,
                    smtp_host=acct.smtp_host,
                    smtp_port=acct.smtp_port,
                    smtp_user=acct.smtp_user,
                    smtp_password=smtp_password,
                )

                connector = IMAPConnector(conn_config, retry_config)

                smtp_client: SMTPClient | None = None
                if acct.smtp_host:
                    smtp_client = SMTPClient(conn_config, retry_config)

                tracker = SyncTracker(acct.id, self._event_ring)

                manager = SyncManager(
                    account=conn_config,
                    account_id=acct.id,
                    connector=connector,
                    folder_repo=folder_repo,
                    mail_repo=mail_repo,
                    attachment_repo=attachment_repo,
                    sync_settings=sync_settings,
                    event_bus=self._event_bus,
                    tracker=tracker,
                    account_repo=account_repo,
                )

                action_propagator = ActionPropagator(
                    connector=connector,
                    retry_config=retry_config,
                    smtp_client=smtp_client,
                )

                idle_watcher = IdleWatcher(
                    connector=connector,
                    sync_settings=sync_settings,
                    retry_config=retry_config,
                    on_new_mail=manager.sync_once.__wrapped__
                    if hasattr(manager.sync_once, "__wrapped__")
                    else self._make_idle_callback(manager),
                )

                self._accounts[acct.name] = AccountSync(
                    account_id=acct.id,
                    connector=connector,
                    manager=manager,
                    idle_watcher=idle_watcher,
                    action_propagator=action_propagator,
                    smtp_client=smtp_client,
                    tracker=tracker,
                )

                await manager.start()
                idle_folders = ["INBOX"]
                await idle_watcher.start(idle_folders)

                logger.info(
                    "Account sync started",
                    extra={"account": acct.name},
                )

            except Exception as exc:
                logger.error(
                    "Failed to start sync for account",
                    extra={
                        "account": acct.name,
                        "error": str(exc),
                    },
                )

    async def stop(self) -> None:
        """Stop sync for all accounts."""
        for name, account_sync in self._accounts.items():
            try:
                await account_sync.idle_watcher.stop()
                await account_sync.manager.stop()
                await account_sync.connector.close()
                logger.info("Account sync stopped", extra={"account": name})
            except Exception as exc:
                logger.warning(
                    "Error stopping account sync",
                    extra={"account": name, "error": str(exc)},
                )

        self._accounts.clear()

    async def add_account(self, acct: Any) -> None:
        """Dynamically add and start sync for a single account."""
        from mail_verdict.core.encryption import decrypt
        from mail_verdict.core.retry import RetryConfig as RC
        from mail_verdict.database.repository import (
            AccountRepository,
            AttachmentRepository,
            FolderRepository,
            MailRepository,
        )
        from mail_verdict.sync.connector import AccountConnConfig

        if acct.name in self._accounts:
            return

        sync_settings = self._settings.get("sync") if self._settings else {}
        retry_settings = self._settings.get("retry") if self._settings else {}
        retry_config = RC.from_settings(retry_settings)

        account_repo = AccountRepository(self._db)
        folder_repo = FolderRepository(self._db)
        mail_repo = MailRepository(self._db)
        attachment_repo = AttachmentRepository(self._db)

        imap_password = decrypt(acct.imap_password) if acct.imap_password else ""
        smtp_password = decrypt(acct.smtp_password) if acct.smtp_password else ""

        conn_config = AccountConnConfig(
            name=acct.name,
            host=acct.imap_host,
            port=acct.imap_port,
            username=acct.imap_user,
            password=imap_password,
            smtp_host=acct.smtp_host,
            smtp_port=acct.smtp_port,
            smtp_user=acct.smtp_user,
            smtp_password=smtp_password,
        )

        connector = IMAPConnector(conn_config, retry_config)

        smtp_client: SMTPClient | None = None
        if acct.smtp_host:
            smtp_client = SMTPClient(conn_config, retry_config)

        tracker = SyncTracker(acct.id, self._event_ring)

        manager = SyncManager(
            account=conn_config,
            account_id=acct.id,
            connector=connector,
            folder_repo=folder_repo,
            mail_repo=mail_repo,
            attachment_repo=attachment_repo,
            sync_settings=sync_settings,
            event_bus=self._event_bus,
            tracker=tracker,
            account_repo=account_repo,
        )

        action_propagator = ActionPropagator(
            connector=connector,
            retry_config=retry_config,
            smtp_client=smtp_client,
        )

        idle_watcher = IdleWatcher(
            connector=connector,
            sync_settings=sync_settings,
            retry_config=retry_config,
            on_new_mail=self._make_idle_callback(manager),
        )

        self._accounts[acct.name] = AccountSync(
            account_id=acct.id,
            connector=connector,
            manager=manager,
            idle_watcher=idle_watcher,
            action_propagator=action_propagator,
            smtp_client=smtp_client,
            tracker=tracker,
        )

        await manager.start()
        await idle_watcher.start(["INBOX"])
        logger.info("Account dynamically added", extra={"account": acct.name})

    async def remove_account(self, name: str) -> None:
        """Stop and remove sync for a single account."""
        if name not in self._accounts:
            return
        account_sync = self._accounts.pop(name)
        try:
            await account_sync.idle_watcher.stop()
            await account_sync.manager.stop()
            await account_sync.connector.close()
            logger.info("Account sync removed", extra={"account": name})
        except Exception as exc:
            logger.warning("Error removing account", extra={"account": name, "error": str(exc)})

    def get_account_sync(self, name: str) -> AccountSync | None:
        """
        Get sync components for a named account.

        Args:
            name: Account name

        Returns:
            AccountSync bundle or None if not found
        """
        return self._accounts.get(name)

    def get_account_sync_by_id(self, account_id: uuid.UUID) -> AccountSync | None:
        """
        Get sync components by account UUID.

        Args:
            account_id: Database UUID for the account

        Returns:
            AccountSync bundle or None if not found
        """
        account_id_str = str(account_id)
        for account_sync in self._accounts.values():
            if str(account_sync.account_id) == account_id_str:
                return account_sync
        return None

    def get_tracker(self, account_id: uuid.UUID) -> SyncTracker | None:
        """
        Get the SyncTracker for an account by UUID.

        Args:
            account_id: Account UUID

        Returns:
            SyncTracker or None if account not found
        """
        account_sync = self.get_account_sync_by_id(account_id)
        if account_sync is not None:
            return account_sync.tracker
        return None

    @staticmethod
    def _make_idle_callback(manager: SyncManager) -> Callable[[str], Coroutine[None, None, None]]:
        """
        Create an IDLE callback that triggers a sync.

        Args:
            manager: SyncManager to trigger
        """

        async def on_new_mail(folder: str) -> None:
            logger.info(
                "IDLE triggered sync",
                extra={
                    "account": manager.account_name,
                    "folder": folder,
                },
            )
            await manager.sync_once()

        return on_new_mail
