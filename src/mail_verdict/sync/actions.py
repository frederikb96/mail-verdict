"""
Action propagator for pushing local actions back to IMAP/SMTP.

Handles IMAP MOVE, COPY, STORE flags, and SMTP forwarding
with retry and error handling.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from mail_verdict.core.retry import RetryConfig
from mail_verdict.sync.connector import IMAPConnector

if TYPE_CHECKING:
    from mail_verdict.sync.extensions import AsyncIMAPExtended
    from mail_verdict.sync.smtp_client import SMTPClient

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of actions to propagate."""

    MOVE = "move"
    COPY = "copy"
    STORE_FLAGS = "store_flags"
    FORWARD = "forward"


@dataclass
class IMAPAction:
    """An action to perform on the IMAP server."""

    action_type: ActionType
    folder: str
    uid_set: str
    target_folder: str | None = None
    flags_add: list[str] | None = None
    flags_remove: list[str] | None = None


@dataclass
class ForwardAction:
    """An email forwarding action."""

    folder: str
    uid: int
    to_address: str
    subject_template: str = "Fwd: {subject}"
    mode: str = "attached"


class ActionPropagator:
    """
    Pushes local actions back to IMAP server and handles SMTP forwarding.

    Actions are retried with exponential backoff on failure.
    Errors are logged but never crash the sync engine.
    """

    def __init__(
        self,
        connector: IMAPConnector,
        retry_config: RetryConfig,
        smtp_client: SMTPClient | None = None,
    ) -> None:
        """
        Initialize action propagator.

        Args:
            connector: IMAP connector for server operations
            retry_config: Retry configuration
            smtp_client: Optional SMTP client for forwarding
        """
        self._connector = connector
        self._smtp_client = smtp_client
        self._retry = retry_config

    async def execute_imap(self, action: IMAPAction) -> bool:
        """
        Execute an IMAP action with retry.

        Args:
            action: Action to perform

        Returns:
            True if action succeeded
        """
        for attempt in range(self._retry.max_retries + 1):
            try:
                async with self._connector.acquire() as conn:
                    # Select the source folder
                    result = await conn.select_plain(action.folder)
                    if not result.ok:
                        raise RuntimeError(f"SELECT {action.folder} failed")

                    if action.action_type == ActionType.MOVE:
                        return await self._do_move(conn, action)
                    elif action.action_type == ActionType.COPY:
                        return await self._do_copy(conn, action)
                    elif action.action_type == ActionType.STORE_FLAGS:
                        return await self._do_store_flags(conn, action)

                    return False

            except Exception as exc:
                if attempt < self._retry.max_retries:
                    delay = self._retry.delay_for_attempt(attempt)
                    logger.warning(
                        "IMAP action failed, retrying",
                        extra={
                            "action": action.action_type.value,
                            "attempt": attempt + 1,
                            "delay": delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "IMAP action failed permanently",
                        extra={
                            "action": action.action_type.value,
                            "folder": action.folder,
                            "uid_set": action.uid_set,
                            "error": str(exc),
                        },
                    )
                    return False

        return False

    async def execute_forward(self, action: ForwardAction) -> bool:
        """
        Forward a mail via SMTP with retry.

        Args:
            action: Forward action details

        Returns:
            True if forwarding succeeded
        """
        if self._smtp_client is None:
            logger.warning("No SMTP client configured, cannot forward")
            return False

        for attempt in range(self._retry.max_retries + 1):
            try:
                # Fetch the message to forward
                async with self._connector.acquire() as conn:
                    result = await conn.select_plain(action.folder)
                    if not result.ok:
                        raise RuntimeError(f"SELECT {action.folder} failed")

                    response = await conn.client.uid("FETCH", str(action.uid), "(RFC822)")
                    if response.result != "OK":
                        raise RuntimeError(f"FETCH UID {action.uid} failed")

                    raw_body: bytes | None = None
                    for line in response.lines:
                        if isinstance(line, bytes) and len(line) > 100:
                            raw_body = line
                            break

                    if raw_body is None:
                        raise RuntimeError("No message body in FETCH response")

                await self._smtp_client.forward(
                    raw_message=raw_body,
                    to_address=action.to_address,
                    subject_template=action.subject_template,
                    mode=action.mode,
                )
                return True

            except Exception as exc:
                if attempt < self._retry.max_retries:
                    delay = self._retry.delay_for_attempt(attempt)
                    logger.warning(
                        "Forward failed, retrying",
                        extra={
                            "uid": action.uid,
                            "attempt": attempt + 1,
                            "delay": delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Forward failed permanently",
                        extra={
                            "uid": action.uid,
                            "to": action.to_address,
                            "error": str(exc),
                        },
                    )
                    return False

        return False

    async def move_to_spam(
        self,
        folder: str,
        uid_set: str,
        spam_folder: str,
        *,
        mark_read: bool = False,
    ) -> bool:
        """
        Move mail to spam folder, set $Junk keyword, optionally mark read.

        Sets flags BEFORE moving so the UID is still valid in the source folder.

        Args:
            folder: Source folder
            uid_set: UID set to move
            spam_folder: Destination spam folder name
            mark_read: Whether to mark as read
        """
        flags_to_add = ["$Junk"]
        if mark_read:
            flags_to_add.append("\\Seen")

        await self.execute_imap(
            IMAPAction(
                action_type=ActionType.STORE_FLAGS,
                folder=folder,
                uid_set=uid_set,
                flags_add=flags_to_add,
            )
        )

        return await self.execute_imap(
            IMAPAction(
                action_type=ActionType.MOVE,
                folder=folder,
                uid_set=uid_set,
                target_folder=spam_folder,
            )
        )

    async def mark_not_spam(
        self,
        folder: str,
        uid_set: str,
    ) -> bool:
        """
        Remove $Junk and set $NotJunk keyword (spam correction).

        Args:
            folder: Current folder containing the mail
            uid_set: UID set to update
        """
        await self.execute_imap(
            IMAPAction(
                action_type=ActionType.STORE_FLAGS,
                folder=folder,
                uid_set=uid_set,
                flags_remove=["$Junk"],
            )
        )
        return await self.execute_imap(
            IMAPAction(
                action_type=ActionType.STORE_FLAGS,
                folder=folder,
                uid_set=uid_set,
                flags_add=["$NotJunk"],
            )
        )

    async def _do_move(
        self,
        conn: AsyncIMAPExtended,
        action: IMAPAction,
    ) -> bool:
        """Execute IMAP UID MOVE, falling back to UID COPY + STORE + EXPUNGE."""
        if not action.target_folder:
            return False

        target = self._quote_folder(action.target_folder)

        # Try UID MOVE first (RFC 6851)
        try:
            response = await conn.client.uid("MOVE", action.uid_set, target)
            if response.result == "OK":
                return True
        except Exception:
            pass

        # Fallback: UID COPY + flag \Deleted + EXPUNGE
        response = await conn.client.uid("COPY", action.uid_set, target)
        if response.result != "OK":
            logger.warning(
                "MOVE failed",
                extra={
                    "uid_set": action.uid_set,
                    "target": action.target_folder,
                    "result": response.result,
                },
            )
            return False

        await conn.client.uid("STORE", action.uid_set, "+FLAGS", "(\\Deleted)")
        await conn.client.expunge()
        return True

    @staticmethod
    def _quote_folder(name: str) -> str:
        """Quote an IMAP folder name if it contains spaces."""
        if " " in name and not name.startswith('"'):
            return f'"{name}"'
        return name

    async def _do_copy(
        self,
        conn: AsyncIMAPExtended,
        action: IMAPAction,
    ) -> bool:
        """Execute IMAP COPY."""
        if not action.target_folder:
            return False

        response = await conn.client.uid("COPY", action.uid_set, action.target_folder)
        ok: bool = response.result == "OK"
        if not ok:
            logger.warning(
                "COPY failed",
                extra={
                    "uid_set": action.uid_set,
                    "target": action.target_folder,
                    "result": response.result,
                },
            )
        return ok

    async def _do_store_flags(
        self,
        conn: AsyncIMAPExtended,
        action: IMAPAction,
    ) -> bool:
        """Execute IMAP STORE for flag changes."""
        ok = True

        if action.flags_add:
            flags = " ".join(action.flags_add)
            response = await conn.client.uid("STORE", action.uid_set, "+FLAGS", f"({flags})")
            if response.result != "OK":
                ok = False

        if action.flags_remove:
            flags = " ".join(action.flags_remove)
            response = await conn.client.uid("STORE", action.uid_set, "-FLAGS", f"({flags})")
            if response.result != "OK":
                ok = False

        return ok
