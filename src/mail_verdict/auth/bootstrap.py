"""
The instance owner, created at startup if missing and given a way in.

The owner row (OWNER_USER_ID) is created by the 0050 migration; this restores
it if it is missing. While the owner has no subject at all -- a new instance,
or one whose owner lost every subject -- the subjects in auth.owner_subjects
are linked to it. Once the owner has any, the users table is the authority:
an admin adds and removes the owner's subjects like anyone else's, and a
subject removed that way stays removed across restarts.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.auth.principal import OWNER_USER_ID
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import User, UserIdentity

logger = logging.getLogger(__name__)


async def ensure_owner(db: DatabaseConnection, owner_subjects: list[str]) -> None:
    """
    Create the owner row if missing; link the owner subjects to it if it
    has none.

    A configured subject that already signs in as another user is left
    there, and the owner gets the rest.

    Args:
        db: The initialized database connection.
        owner_subjects: auth.owner_subjects.
    """
    async with db.session() as session:
        await session.execute(
            pg_insert(User)
            .values(id=OWNER_USER_ID, display_name="Owner", role="admin")
            .on_conflict_do_nothing(index_elements=[User.id])
        )
        held = await session.scalar(
            select(func.count()).select_from(UserIdentity).where(
                UserIdentity.user_id == OWNER_USER_ID
            )
        )
        if held:
            return
        for subject in owner_subjects:
            linked = await session.scalar(
                pg_insert(UserIdentity)
                .values(subject=subject, user_id=OWNER_USER_ID)
                .on_conflict_do_nothing(index_elements=[UserIdentity.subject])
                .returning(UserIdentity.subject)
            )
            if linked is None:
                logger.warning(
                    "An auth.owner_subjects entry already signs in as another user; "
                    "it was not linked to the owner"
                )
        logger.info("Linked the owner, who had no subject, to auth.owner_subjects")
