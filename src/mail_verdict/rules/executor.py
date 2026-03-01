"""
Rule action executor.

Executes actions defined in rule configs by delegating to
ActionPropagator, TagRepository, and SMTPClient.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from mail_verdict.database.models import TagSource
from mail_verdict.rules.conditions import MailContext
from mail_verdict.sync.actions import (
    ActionPropagator,
    ActionType,
    ForwardAction,
    IMAPAction,
)

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
    Executes rule actions against IMAP, database, and SMTP.

    Each action type maps to a handler method that delegates
    to the appropriate service (ActionPropagator, TagRepository, etc.).
    """

    def __init__(
        self,
        propagator: ActionPropagator | None = None,
        tag_repo: Any | None = None,
        notify_callback: Any | None = None,
        folder_repo: Any | None = None,
    ) -> None:
        """
        Initialize action executor with service dependencies.

        Args:
            propagator: IMAP action propagator
            tag_repo: TagRepository for tag persistence
            notify_callback: Async callback for notify actions (emits SSE events)
            folder_repo: FolderRepository for dynamic folder lookups
        """
        self._propagator = propagator
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
            mail_id: Database mail UUID (needed for tag operations)
            uid: IMAP UID for IMAP operations

        Returns:
            ActionResult with success status

        Raises:
            StopProcessing: When action is "stop"
        """
        # Detect action type from the dict key
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

    async def _action_move_to(
        self,
        folder: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Move mail to target folder via IMAP."""
        if not self._propagator:
            logger.warning("No ActionPropagator configured, skipping move")
            return
        await self._propagator.execute_imap(
            IMAPAction(
                action_type=ActionType.MOVE,
                folder=ctx.folder,
                uid_set=str(uid),
                target_folder=folder,
            )
        )

    async def _action_copy_to(
        self,
        folder: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Copy mail to target folder via IMAP."""
        if not self._propagator:
            logger.warning("No ActionPropagator configured, skipping copy")
            return
        await self._propagator.execute_imap(
            IMAPAction(
                action_type=ActionType.COPY,
                folder=ctx.folder,
                uid_set=str(uid),
                target_folder=folder,
            )
        )

    async def _action_mark_as(
        self,
        value: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Mark mail as read or unread via IMAP STORE flags."""
        if not self._propagator:
            return
        if value == "read":
            await self._propagator.execute_imap(
                IMAPAction(
                    action_type=ActionType.STORE_FLAGS,
                    folder=ctx.folder,
                    uid_set=str(uid),
                    flags_add=["\\Seen"],
                )
            )
        elif value == "unread":
            await self._propagator.execute_imap(
                IMAPAction(
                    action_type=ActionType.STORE_FLAGS,
                    folder=ctx.folder,
                    uid_set=str(uid),
                    flags_remove=["\\Seen"],
                )
            )

    async def _action_star(
        self,
        value: Any,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Star a mail via IMAP STORE flags."""
        if not self._propagator:
            return
        await self._propagator.execute_imap(
            IMAPAction(
                action_type=ActionType.STORE_FLAGS,
                folder=ctx.folder,
                uid_set=str(uid),
                flags_add=["\\Flagged"],
            )
        )

    async def _action_unstar(
        self,
        value: Any,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Unstar a mail via IMAP STORE flags."""
        if not self._propagator:
            return
        await self._propagator.execute_imap(
            IMAPAction(
                action_type=ActionType.STORE_FLAGS,
                folder=ctx.folder,
                uid_set=str(uid),
                flags_remove=["\\Flagged"],
            )
        )

    async def _action_tag(
        self,
        tag_name: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Add a tag to the mail in Postgres + best-effort IMAP keyword sync."""
        if self._tag_repo and mail_id:
            await self._tag_repo.add_tag(mail_id, tag_name, TagSource.RULE)
        # Best-effort IMAP keyword sync
        if self._propagator:
            await self._propagator.execute_imap(
                IMAPAction(
                    action_type=ActionType.STORE_FLAGS,
                    folder=ctx.folder,
                    uid_set=str(uid),
                    flags_add=[tag_name],
                )
            )

    async def _action_remove_tag(
        self,
        tag_name: str,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Remove a tag from the mail."""
        if self._tag_repo and mail_id:
            await self._tag_repo.remove_tag(mail_id, tag_name)
        if self._propagator:
            await self._propagator.execute_imap(
                IMAPAction(
                    action_type=ActionType.STORE_FLAGS,
                    folder=ctx.folder,
                    uid_set=str(uid),
                    flags_remove=[tag_name],
                )
            )

    async def _action_forward_to(
        self,
        value: str | dict[str, str],
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Forward the mail via SMTP."""
        if not self._propagator:
            logger.warning("No ActionPropagator configured, skipping forward")
            return

        if isinstance(value, dict):
            address = value.get("address", "")
            subject_template = value.get(
                "subject_rewrite",
                "Fwd: {subject}",
            )
        else:
            address = str(value)
            subject_template = "Fwd: {subject}"

        subject_rendered = _render_template(subject_template, ctx)
        await self._propagator.execute_forward(
            ForwardAction(
                folder=ctx.folder,
                uid=uid,
                to_address=address,
                subject_template=subject_rendered,
            )
        )

    async def _action_trash(
        self,
        value: Any,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Move mail to Trash folder."""
        if not self._propagator:
            return
        await self._propagator.execute_imap(
            IMAPAction(
                action_type=ActionType.MOVE,
                folder=ctx.folder,
                uid_set=str(uid),
                target_folder="Trash",
            )
        )

    _SPAM_FOLDER_FALLBACKS = ["Junk", "Spam", "Bulk Mail"]

    async def _action_move_to_spam(
        self,
        value: Any,
        ctx: MailContext,
        *,
        mail_id: uuid.UUID | None = None,
        uid: int = 0,
    ) -> None:
        """Move mail to spam folder via ActionPropagator convenience method."""
        if not self._propagator:
            return
        spam_folder = self._SPAM_FOLDER_FALLBACKS[0]
        if self._folder_repo and mail_id:
            spam_folder = await self._resolve_spam_folder(mail_id) or spam_folder
        await self._propagator.move_to_spam(
            folder=ctx.folder,
            uid_set=str(uid),
            spam_folder=spam_folder,
        )

    async def _resolve_spam_folder(self, mail_id: uuid.UUID) -> str | None:
        """Look up the spam folder name for the account owning this mail."""
        try:
            from sqlalchemy import select

            from mail_verdict.database.models import Mail, SpecialUse

            folder_repo = self._folder_repo
            if folder_repo is None or not hasattr(folder_repo, "_db"):
                return None
            async with folder_repo._db.session() as session:
                result = await session.execute(select(Mail.account_id).where(Mail.id == mail_id))
                account_id = result.scalar_one_or_none()
                if not account_id:
                    return None

            folders = await folder_repo.get_by_account(account_id)
            for f in folders:
                if f.special_use and f.special_use == SpecialUse.JUNK:
                    return str(f.imap_name)
            folder_names = {f.imap_name for f in folders}
            for name in self._SPAM_FOLDER_FALLBACKS:
                if name in folder_names:
                    return name
        except Exception:
            pass
        return None

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
