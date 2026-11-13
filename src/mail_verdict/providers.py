"""
Mail providers whose folder layout needs handling on MailVerdict's side.

Gmail exposes labels as IMAP folders and `[Gmail]/All Mail` as the `all`
role, so one message appears in its label folders, in Starred, in All
Mail and in the inbox at once. PostIMAP mirrors every copy faithfully;
what MailVerdict does with the copies is set here, once per folder.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.postimap.listener import PostimapEvent

logger = logging.getLogger(__name__)

GMAIL_IMAP_HOSTS: tuple[str, ...] = ("imap.gmail.com", "imap.googlemail.com")

# Roles whose folders hold mail of their own on Gmail; every other folder
# (labels, Starred, Important) holds copies.
_OWN_MAIL_ROLES = ("inbox", "sent", "drafts", "trash", "junk", "archive", "all")

# Fills only what the user has not set: an override or a switch already
# present is kept, so a later choice survives the next run. `all` becomes
# the Archive on every server -- a folder holding everything is never a
# place new mail lands. On a Gmail account every folder that is not one of
# its own-mail roles stops running the pipeline and the semantic index.
_APPLY_DEFAULTS_SQL = f"""
INSERT INTO folder_prefs (folder_id, special_use_override, pipeline_enabled)
SELECT f.id,
       CASE WHEN f.special_use = 'all' THEN 'archive' END,
       CASE WHEN a.imap_host = ANY(:gmail_hosts)
                 AND coalesce(f.special_use, '') NOT IN
                     ({", ".join(f"'{r}'" for r in _OWN_MAIL_ROLES)})
            THEN false END
FROM folders f
JOIN accounts a ON a.id = f.account_id
WHERE f.deleted_at IS NULL
  AND {{scope}}
  AND (f.special_use = 'all'
       OR (a.imap_host = ANY(:gmail_hosts)
           AND coalesce(f.special_use, '') NOT IN
               ({", ".join(f"'{r}'" for r in _OWN_MAIL_ROLES)})))
ON CONFLICT (folder_id) DO UPDATE SET
    special_use_override = coalesce(
        folder_prefs.special_use_override, EXCLUDED.special_use_override),
    pipeline_enabled = coalesce(folder_prefs.pipeline_enabled, EXCLUDED.pipeline_enabled)
"""


async def apply_folder_defaults(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
) -> None:
    """Set the provider defaults for one folder. Idempotent."""
    await session.execute(
        text(_APPLY_DEFAULTS_SQL.format(scope="f.id = :folder_id AND f.account_id = :account_id")),
        {"folder_id": folder_id, "account_id": account_id, "gmail_hosts": list(GMAIL_IMAP_HOSTS)},
    )


async def apply_folder_defaults_for_event(db: DatabaseConnection, event: PostimapEvent) -> None:
    """Apply the defaults to the folder a `folder` event names -- on the
    folder's insert, and again when its first sync completes in case the
    insert event was missed."""
    try:
        folder_id = uuid.UUID(event.folder_id) if event.folder_id else uuid.UUID(event.id)
        account_id = uuid.UUID(event.account_id)
    except ValueError:
        logger.warning("Invalid folder event payload: %s", event)
        return
    async with db.session() as session:
        await apply_folder_defaults(session, account_id=account_id, folder_id=folder_id)
