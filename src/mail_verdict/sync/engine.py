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

if TYPE_CHECKING:
    from mail_verdict.config import AccountConfig, MailVerdictConfig
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.rules.bus import EventBus

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
        """
        self.account_id = account_id
        self.connector = connector
        self.manager = manager
        self.idle_watcher = idle_watcher
        self.action_propagator = action_propagator
        self.smtp_client = smtp_client


class SyncEngine:
    """
    Top-level sync engine managing all account syncs.

    Creates and manages per-account sync infrastructure:
    connector, sync manager, IDLE watcher, action propagator, SMTP client.
    """

    def __init__(
        self,
        config: MailVerdictConfig,
        db: DatabaseConnection,
        event_bus: EventBus | None = None,
    ) -> None:
        """
        Initialize sync engine.

        Args:
            config: Global configuration
            db: Database connection for repository access
            event_bus: Optional event bus for broadcasting sync events
        """
        self._config = config
        self._db = db
        self._event_bus = event_bus
        self._accounts: dict[str, AccountSync] = {}

    async def start(self) -> None:
        """Start sync for all configured accounts."""
        from mail_verdict.database.repository import (
            AttachmentRepository,
            FolderRepository,
            MailRepository,
        )

        if not self._config.accounts:
            logger.info("No accounts configured, sync engine idle")
            return

        folder_repo = FolderRepository(self._db)
        mail_repo = MailRepository(self._db)
        attachment_repo = AttachmentRepository(self._db)

        for account_cfg in self._config.accounts:
            try:
                account_id = await self._ensure_account(account_cfg)

                connector = IMAPConnector(account_cfg, self._config.retry)

                smtp_client: SMTPClient | None = None
                if account_cfg.smtp_host:
                    smtp_client = SMTPClient(account_cfg, self._config.retry)

                manager = SyncManager(
                    account=account_cfg,
                    account_id=account_id,
                    connector=connector,
                    folder_repo=folder_repo,
                    mail_repo=mail_repo,
                    attachment_repo=attachment_repo,
                    config=self._config,
                    event_bus=self._event_bus,
                )

                action_propagator = ActionPropagator(
                    connector=connector,
                    retry_config=self._config.retry,
                    smtp_client=smtp_client,
                )

                idle_watcher = IdleWatcher(
                    connector=connector,
                    sync_config=self._config.sync,
                    retry_config=self._config.retry,
                    on_new_mail=manager.sync_once.__wrapped__
                    if hasattr(manager.sync_once, "__wrapped__")
                    else self._make_idle_callback(manager),
                )

                self._accounts[account_cfg.name] = AccountSync(
                    account_id=account_id,
                    connector=connector,
                    manager=manager,
                    idle_watcher=idle_watcher,
                    action_propagator=action_propagator,
                    smtp_client=smtp_client,
                )

                await manager.start()
                await idle_watcher.start(account_cfg.idle_folders)

                logger.info(
                    "Account sync started",
                    extra={"account": account_cfg.name},
                )

            except Exception as exc:
                logger.error(
                    "Failed to start sync for account",
                    extra={
                        "account": account_cfg.name,
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

    def get_account_sync(self, name: str) -> AccountSync | None:
        """
        Get sync components for a named account.

        Args:
            name: Account name

        Returns:
            AccountSync bundle or None if not found
        """
        return self._accounts.get(name)

    async def _ensure_account(self, account_cfg: AccountConfig) -> uuid.UUID:
        """
        Ensure account exists in database, return its UUID.

        Creates the account row if it doesn't exist.

        Args:
            account_cfg: Account configuration
        """
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from mail_verdict.database.models import Account

        async with self._db.session() as session:
            check_result = await session.execute(
                select(Account).where(Account.name == account_cfg.name)
            )
            existing = check_result.scalar_one_or_none()
            if existing:
                return existing.id

            # Create new account
            stmt = (
                pg_insert(Account)
                .values(
                    name=account_cfg.name,
                    imap_host=account_cfg.host,
                    imap_port=account_cfg.port,
                    imap_user=account_cfg.username,
                    smtp_host=account_cfg.smtp_host,
                    smtp_port=account_cfg.smtp_port,
                    smtp_user=account_cfg.smtp_user,
                )
                .on_conflict_do_nothing(index_elements=["name"])
                .returning(Account.id)
            )
            insert_result = await session.execute(stmt)
            new_id: uuid.UUID | None = insert_result.scalar_one_or_none()
            if new_id:
                return new_id

            # Race condition: created between our check and insert
            race_result = await session.execute(
                select(Account).where(Account.name == account_cfg.name)
            )
            return race_result.scalar_one().id

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
