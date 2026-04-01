"""
Rule action executor.

Executes actions defined in rule configs by delegating to
direct SQL UPDATEs (PostIMAP triggers handle IMAP propagation),
TagRepository, and notification callbacks.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from mail_verdict.database.models import TagSource
from mail_verdict.rules.conditions import MailContext

logger = logging.getLogger(__name__)

TEMPLATE_VARS = ("from", "to", "subject", "date", "folder", "tags")


class StopProcessing(Exception):
    """Raised when a 'stop' action halts further rule processing."""


@dataclass
class ActionResult:
    """Outcome of a single action execution."""

    action_type: str
    success: bool
    error: str | None = None


def _render_template(template: str, ctx: MailContext) -> str:
    """
    Render a template string with mail context variables.

    Supported variables: {from}, {to}, {subject}, {date}, {folder}, {tags}
    Missing variables are left as-is.

    Args:
        template: Template string with {var} placeholders
        ctx: Mail context for variable values
    """
    values = {
        "from": ctx.from_addr,
        "to": ", ".join(ctx.to_addrs) if ctx.to_addrs else "",
        "subject": ctx.subject,
        "date": "",
        "folder": ctx.folder,
        "tags": ", ".join(ctx.tags),
    }

    class _SafeDict(dict):  # type: ignore[type-arg]
        def __missing__(self, key: str) -> str:
            return f"{{{key}}}"

    return template.format_map(_SafeDict(values))


class ActionExecutor:
    """
    Executes rule actions via direct SQL UPDATEs.

    Each action type maps to a handler method that updates the local
    DB state. PostIMAP PG triggers handle outbound IMAP propagation.
    """

    def __init__(
        self,
        tag_repo: Any | None = None,
        notify_callback: Any | None = None,
        folder_repo: Any | None = None,
    ) -> None:
        """
        Initialize action executor with service dependencies.

        Args:
            tag_repo: TagRepository for tag persistence
            notify_callback: Async callback for notify actions (emits SSE events)
            folder_repo: FolderRepository for dynamic folder lookups
        """
        self._tag_repo = tag_repo
        self._notify_callback = notify_callback
        self._folder_repo = folder_repo

    async def execute(
        self,
        action: dict[str, Any],
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> ActionResult:
        """
        Execute a single rule action.

        Args:
            action: Action config dict (e.g. {"move_to": "Archive"})
            ctx: Mail context for template rendering
            mail_id: Database message UUID (needed for tag operations)
            uid: IMAP UID (kept for interface compatibility)

        Returns:
            ActionResult with success status

        Raises:
            StopProcessing: When action is "stop"
        """
        action_type, value = self._extract_action(action)

        handler = getattr(self, f"_action_{action_type}", None)
        if handler is None:
            logger.warning("Unknown action type", extra={"action": action_type})
            return ActionResult(action_type=action_type, success=False, error="Unknown action")

        try:
            await handler(value, ctx, mail_id=mail_id, uid=uid)
            return ActionResult(action_type=action_type, success=True)
        except StopProcessing:
            raise
        except Exception as exc:
            logger.error(
                "Action execution failed",
                extra={"action": action_type, "error": str(exc)},
            )
            return ActionResult(action_type=action_type, success=False, error=str(exc))

    def _extract_action(self, action: dict[str, Any]) -> tuple[str, Any]:
        """
        Extract the action type and value from a config dict.

        Args:
            action: Action dict (e.g. {"move_to": "Archive"} or {"stop": true})

        Returns:
            Tuple of (action_type, value)
        """
        for key, value in action.items():
            return key, value
        raise ValueError("Empty action dict")

    async def _resolve_account_id(self, mail_id: uuid.UUID) -> uuid.UUID | None:
        """
        Look up the account_id for a message.

        Args:
            mail_id: Message UUID

        Returns:
            Account UUID or None if not found
        """
        from sqlalchemy import select

        from mail_verdict.database.connection import get_db_connection
        from mail_verdict.database.models import Message

        db = get_db_connection()
        async with db.session() as session:
            result = await session.execute(
                select(Message.account_id).where(Message.id == mail_id)
            )
            return result.scalar_one_or_none()

    async def _resolve_folder_id_by_name(
        self, account_id: uuid.UUID, folder_name: str,
    ) -> uuid.UUID | None:
        """
        Resolve a folder UUID from an IMAP folder name.

        Args:
            account_id: Account UUID
            folder_name: IMAP folder name

        Returns:
            Folder UUID or None if not found
        """
        if not self._folder_repo:
            return None
        folders = await self._folder_repo.get_by_account(account_id)
        target = next((f for f in folders if f.imap_name == folder_name), None)
        return target.id if target else None

    async def _resolve_special_folder(
        self, account_id: uuid.UUID, special_use: str,
    ) -> uuid.UUID | None:
        """
        Resolve a special-use folder UUID by querying Folder.special_use.

        Args:
            account_id: Account UUID
            special_use: Special use value (e.g. "junk", "trash")

        Returns:
            Folder UUID or None if not found
        """
        from sqlalchemy import select

        from mail_verdict.database.connection import get_db_connection
        from mail_verdict.database.models import Folder

        db = get_db_connection()
        async with db.session() as session:
            result = await session.execute(
                select(Folder.id).where(
                    Folder.account_id == account_id,
                    Folder.special_use == special_use,
                ).limit(1)
            )
            return result.scalar_one_or_none()

    async def _action_move_to(
        self,
        folder: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Move message to target folder via direct SQL UPDATE."""
        if not mail_id:
            logger.warning("No mail_id for move_to action, skipping")
            return

        account_id = await self._resolve_account_id(mail_id)
        if not account_id:
            return

        target_folder_id = await self._resolve_folder_id_by_name(account_id, folder)
        if not target_folder_id:
            logger.warning("Target folder not found", extra={"folder": folder})
            return

        from sqlalchemy import update

        from mail_verdict.database.connection import get_db_connection
        from mail_verdict.database.models import Message

        db = get_db_connection()
        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == mail_id)
                .values(folder_id=target_folder_id)
            )

    async def _action_copy_to(
        self,
        folder: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Copy message to target folder (not yet supported in PostIMAP mode)."""
        # Copy requires creating a new message row -- not yet supported.
        logger.warning(
            "Copy action not yet supported in PostIMAP mode, skipping",
            extra={"mail_id": str(mail_id) if mail_id else None},
        )

    async def _action_mark_as(
        self,
        value: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Mark message as read or unread via direct SQL UPDATE."""
        if not mail_id:
            return

        from sqlalchemy import update

        from mail_verdict.database.connection import get_db_connection
        from mail_verdict.database.models import Message

        db = get_db_connection()
        if value == "read":
            async with db.session() as session:
                await session.execute(
                    update(Message).where(Message.id == mail_id).values(is_seen=True)
                )
        elif value == "unread":
            async with db.session() as session:
                await session.execute(
                    update(Message).where(Message.id == mail_id).values(is_seen=False)
                )

    async def _action_star(
        self,
        value: Any,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Star a message via direct SQL UPDATE."""
        if not mail_id:
            return

        from sqlalchemy import update

        from mail_verdict.database.connection import get_db_connection
        from mail_verdict.database.models import Message

        db = get_db_connection()
        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == mail_id).values(is_flagged=True)
            )

    async def _action_unstar(
        self,
        value: Any,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Unstar a message via direct SQL UPDATE."""
        if not mail_id:
            return

        from sqlalchemy import update

        from mail_verdict.database.connection import get_db_connection
        from mail_verdict.database.models import Message

        db = get_db_connection()
        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == mail_id).values(is_flagged=False)
            )

    async def _action_tag(
        self,
        tag_name: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Add a tag to the message in Postgres."""
        if self._tag_repo and mail_id:
            await self._tag_repo.add_tag(mail_id, tag_name, TagSource.RULE)

    async def _action_remove_tag(
        self,
        tag_name: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Remove a tag from the message in Postgres."""
        if self._tag_repo and mail_id:
            await self._tag_repo.remove_tag(mail_id, tag_name)

    async def _action_forward_to(
        self,
        value: str | dict[str, str],
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Forward the message via SMTP (not yet supported)."""
        logger.warning(
            "Forward action not yet supported, skipping",
            extra={"mail_id": str(mail_id) if mail_id else None},
        )

    async def _action_trash(
        self,
        value: Any,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Move message to Trash folder via direct SQL UPDATE."""
        if not mail_id:
            return

        account_id = await self._resolve_account_id(mail_id)
        if not account_id:
            return

        trash_folder_id = await self._resolve_special_folder(account_id, "trash")
        if not trash_folder_id:
            logger.warning("No trash folder found, skipping trash action")
            return

        from sqlalchemy import update

        from mail_verdict.database.connection import get_db_connection
        from mail_verdict.database.models import Message

        db = get_db_connection()
        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == mail_id)
                .values(folder_id=trash_folder_id)
            )

    async def _action_move_to_spam(
        self,
        value: Any,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Move message to spam folder via direct SQL UPDATE."""
        if not mail_id:
            return

        account_id = await self._resolve_account_id(mail_id)
        if not account_id:
            return

        spam_folder_id = await self._resolve_special_folder(account_id, "junk")
        if not spam_folder_id:
            logger.warning("No spam folder found, skipping move_to_spam action")
            return

        from sqlalchemy import update

        from mail_verdict.database.connection import get_db_connection
        from mail_verdict.database.models import Message

        db = get_db_connection()
        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == mail_id)
                .values(folder_id=spam_folder_id)
            )

    async def _action_notify(
        self,
        message_template: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Emit an SSE notification event."""
        rendered = _render_template(message_template, ctx)
        logger.info(
            "Rule notification",
            extra={"message": rendered, "mail_id": str(mail_id)},
        )
        if self._notify_callback:
            await self._notify_callback(rendered, mail_id=mail_id)

    async def _action_stop(
        self,
        value: Any,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Stop processing further rules."""
        raise StopProcessing()
